#
# Copyright (c) 2012-2021 Snowflake Computing Inc. All rights reserved.
#

from __future__ import annotations

import collections.abc
import os
import random
import string
import warnings
from functools import partial
from logging import getLogger
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING, Callable, Iterable, Iterator, Sequence, TypeVar

from typing_extensions import Literal

from snowflake.connector import ProgrammingError
from snowflake.connector.options import pandas
from snowflake.connector.telemetry import TelemetryData, TelemetryField

if TYPE_CHECKING:  # pragma: no cover
    from .connection import SnowflakeConnection

    try:
        import sqlalchemy
    except ImportError:
        sqlalchemy = None

T = TypeVar("T", bound=collections.abc.Sequence)

logger = getLogger(__name__)


def chunk_helper(lst: T, n: int) -> Iterator[tuple[int, T]]:
    """Helper generator to chunk a sequence efficiently with current index like if enumerate was called on sequence."""
    for i in range(0, len(lst), n):
        yield int(i / n), lst[i : i + n]


def write_pandas(
    conn: SnowflakeConnection,
    df: pandas.DataFrame,
    table_name: str,
    database: str | None = None,
    schema: str | None = None,
    chunk_size: int | None = None,
    compression: str = "gzip",
    on_error: str = "abort_statement",
    parallel: int = 4,
    quote_identifiers: bool = True,
    auto_create_table: bool = False,
    create_temp_table: bool = False,
    overwrite: bool = False,
    table_type: Literal["", "temp", "temporary", "transient"] = "",
) -> tuple[
    bool,
    int,
    int,
    Sequence[
        tuple[
            str,
            str,
            int,
            int,
            int,
            int,
            str | None,
            int | None,
            int | None,
            str | None,
        ]
    ],
]:
    """Allows users to most efficiently write back a pandas DataFrame to Snowflake.

    It works by dumping the DataFrame into Parquet files, uploading them and finally copying their data into the table.

    Returns whether all files were ingested correctly, number of chunks uploaded, and number of rows ingested
    with all of the COPY INTO command's output for debugging purposes.

        Example usage:
            import pandas
            from snowflake.connector.pandas_tools import write_pandas

            df = pandas.DataFrame([('Mark', 10), ('Luke', 20)], columns=['name', 'balance'])
            success, nchunks, nrows, _ = write_pandas(cnx, df, 'customers')

    Args:
        conn: Connection to be used to communicate with Snowflake.
        df: Dataframe we'd like to write back.
        table_name: Table name where we want to insert into.
        database: Database schema and table is in, if not provided the default one will be used (Default value = None).
        schema: Schema table is in, if not provided the default one will be used (Default value = None).
        chunk_size: Number of elements to be inserted once, if not provided all elements will be dumped once
            (Default value = None).
        compression: The compression used on the Parquet files, can only be gzip, or snappy. Gzip gives supposedly a
            better compression, while snappy is faster. Use whichever is more appropriate (Default value = 'gzip').
        on_error: Action to take when COPY INTO statements fail, default follows documentation at:
            https://docs.snowflake.com/en/sql-reference/sql/copy-into-table.html#copy-options-copyoptions
            (Default value = 'abort_statement').
        parallel: Number of threads to be used when uploading chunks, default follows documentation at:
            https://docs.snowflake.com/en/sql-reference/sql/put.html#optional-parameters (Default value = 4).
        quote_identifiers: By default, identifiers, specifically database, schema, table and column names
            (from df.columns) will be quoted. If set to False, identifiers are passed on to Snowflake without quoting.
            I.e. identifiers will be coerced to uppercase by Snowflake.  (Default value = True)
        auto_create_table: When true, will automatically create a table with corresponding columns for each column in
            the passed in DataFrame. The table will not be created if it already exists
        create_temp_table: (Deprecated) Will make the auto-created table as a temporary table
        overwrite: When true, and if auto_create_table is true, then it drops the table. Otherwise, it
        truncates the table. In both cases it will replace the existing contents of the table with that of the passed in
            Pandas DataFrame.
        table_type: The table type of to-be-created table. The supported table types include ``temp``/``temporary``
            and ``transient``. Empty means permanent table as per SQL convention.

    Returns:
        Returns the COPY INTO command's results to verify ingestion in the form of a tuple of whether all chunks were
        ingested correctly, # of chunks, # of ingested rows, and ingest's output.
    """
    if database is not None and schema is None:
        raise ProgrammingError(
            "Schema has to be provided to write_pandas when a database is provided"
        )
    # This dictionary maps the compression algorithm to Snowflake put copy into command type
    # https://docs.snowflake.com/en/sql-reference/sql/copy-into-table.html#type-parquet
    compression_map = {"gzip": "auto", "snappy": "snappy"}
    if compression not in compression_map.keys():
        raise ProgrammingError(
            "Invalid compression '{}', only acceptable values are: {}".format(
                compression, compression_map.keys()
            )
        )

    if create_temp_table:
        warnings.warn(
            "create_temp_table is deprecated, we still respect this parameter when it is True but "
            'please consider using `table_type="temp"` instead',
            DeprecationWarning,
            # warnings.warn -> write_pandas
            stacklevel=2,
        )
        table_type = "temp"

    if table_type and table_type.lower() not in ["temp", "temporary", "transient"]:
        raise ValueError(
            "Unsupported table type. Expected table types: temp/temporary, transient"
        )

    if quote_identifiers:
        location = (
            (('"' + database + '".') if database else "")
            + (('"' + schema + '".') if schema else "")
            + ('"' + table_name + '"')
        )
    else:
        location = (
            (database + "." if database else "")
            + (schema + "." if schema else "")
            + (table_name)
        )
    if chunk_size is None:
        chunk_size = len(df)
    cursor = conn.cursor()
    stage_name = None  # Forward declaration
    while True:
        try:
            stage_name = "".join(
                random.choice(string.ascii_lowercase) for _ in range(5)
            )
            create_stage_sql = (
                "create temporary stage /* Python:snowflake.connector.pandas_tools.write_pandas() */ "
                '"{stage_name}"'
            ).format(stage_name=stage_name)
            logger.debug(f"creating stage with '{create_stage_sql}'")
            cursor.execute(create_stage_sql, _is_internal=True).fetchall()
            break
        except ProgrammingError as pe:
            if pe.msg.endswith("already exists."):
                continue
            raise

    with TemporaryDirectory() as tmp_folder:
        for i, chunk in chunk_helper(df, chunk_size):
            chunk_path = os.path.join(tmp_folder, f"file{i}.txt")
            # Dump chunk into parquet file
            chunk.to_parquet(chunk_path, compression=compression)
            # Upload parquet file
            upload_sql = (
                "PUT /* Python:snowflake.connector.pandas_tools.write_pandas() */ "
                "'file://{path}' @\"{stage_name}\" PARALLEL={parallel}"
            ).format(
                path=chunk_path.replace("\\", "\\\\").replace("'", "\\'"),
                stage_name=stage_name,
                parallel=parallel,
            )
            logger.debug(f"uploading files with '{upload_sql}'")
            cursor.execute(upload_sql, _is_internal=True)
            # Remove chunk file
            os.remove(chunk_path)
    if quote_identifiers:
        columns = '"' + '","'.join(list(df.columns)) + '"'
    else:
        columns = ",".join(list(df.columns))

    if overwrite:
        if auto_create_table:
            drop_table_sql = f"DROP TABLE IF EXISTS {location} /* Python:snowflake.connector.pandas_tools.write_pandas() */ "
            logger.debug(f"dropping table with '{drop_table_sql}'")
            cursor.execute(drop_table_sql, _is_internal=True)
        else:
            truncate_table_sql = f"TRUNCATE TABLE IF EXISTS {location} /* Python:snowflake.connector.pandas_tools.write_pandas() */ "
            logger.debug(f"truncating table with '{truncate_table_sql}'")
            cursor.execute(truncate_table_sql, _is_internal=True)

    if auto_create_table:
        file_format_name = None
        while True:
            try:
                file_format_name = (
                    '"'
                    + "".join(random.choice(string.ascii_lowercase) for _ in range(5))
                    + '"'
                )
                file_format_sql = (
                    f"CREATE FILE FORMAT {file_format_name} "
                    f"/* Python:snowflake.connector.pandas_tools.write_pandas() */ "
                    f"TYPE=PARQUET COMPRESSION={compression_map[compression]}"
                )
                logger.debug(f"creating file format with '{file_format_sql}'")
                cursor.execute(file_format_sql, _is_internal=True)
                break
            except ProgrammingError as pe:
                if pe.msg.endswith("already exists."):
                    continue
                raise
        infer_schema_sql = f"SELECT COLUMN_NAME, TYPE FROM table(infer_schema(location=>'@\"{stage_name}\"', file_format=>'{file_format_name}'))"
        logger.debug(f"inferring schema with '{infer_schema_sql}'")
        column_type_mapping = dict(
            cursor.execute(infer_schema_sql, _is_internal=True).fetchall()
        )
        # Infer schema can return the columns out of order depending on the chunking we do when uploading
        # so we have to iterate through the dataframe columns to make sure we create the table with its
        # columns in order
        quote = '"' if quote_identifiers else ""
        create_table_columns = ", ".join(
            [f"{quote}{c}{quote} {column_type_mapping[c]}" for c in df.columns]
        )
        create_table_sql = (
            f"CREATE {table_type.upper()} TABLE IF NOT EXISTS {location} "
            f"({create_table_columns})"
            f" /* Python:snowflake.connector.pandas_tools.write_pandas() */ "
        )
        logger.debug(f"auto creating table with '{create_table_sql}'")
        cursor.execute(create_table_sql, _is_internal=True)
        drop_file_format_sql = f"DROP FILE FORMAT IF EXISTS {file_format_name}"
        logger.debug(f"dropping file format with '{drop_file_format_sql}'")
        cursor.execute(drop_file_format_sql, _is_internal=True)

    # in Snowflake, all parquet data is stored in a single column, $1, so we must select columns explicitly
    # see (https://docs.snowflake.com/en/user-guide/script-data-load-transform-parquet.html)
    if quote_identifiers:
        parquet_columns = "$1:" + ",$1:".join(f'"{c}"' for c in df.columns)
    else:
        parquet_columns = "$1:" + ",$1:".join(df.columns)

    copy_into_sql = (
        "COPY INTO {location} /* Python:snowflake.connector.pandas_tools.write_pandas() */ "
        "({columns}) "
        'FROM (SELECT {parquet_columns} FROM @"{stage_name}") '
        "FILE_FORMAT=(TYPE=PARQUET COMPRESSION={compression}) "
        "PURGE=TRUE ON_ERROR={on_error}"
    ).format(
        location=location,
        columns=columns,
        parquet_columns=parquet_columns,
        stage_name=stage_name,
        compression=compression_map[compression],
        on_error=on_error,
    )
    logger.debug(f"copying into with '{copy_into_sql}'")
    copy_results = cursor.execute(copy_into_sql, _is_internal=True).fetchall()
    cursor._log_telemetry_job_data(TelemetryField.PANDAS_WRITE, TelemetryData.TRUE)
    cursor.close()
    return (
        all(e[1] == "LOADED" for e in copy_results),
        len(copy_results),
        sum(int(e[3]) for e in copy_results),
        copy_results,
    )


def make_pd_writer(
    quote_identifiers: bool = True,
) -> Callable[
    [
        pandas.io.sql.SQLTable,
        sqlalchemy.engine.Engine | sqlalchemy.engine.Connection,
        Iterable,
        Iterable,
    ],
    None,
]:
    """This returns a pd_writer with the desired arguments.

        Example usage:
            import pandas as pd
            from snowflake.connector.pandas_tools import pd_writer

            sf_connector_version_df = pd.DataFrame([('snowflake-connector-python', '1.0')], columns=['NAME', 'NEWEST_VERSION'])
            sf_connector_version_df.to_sql('driver_versions', engine, index=False, method=make_pd_writer())

            # to use quote_identifiers=False,
            from functools import partial
            sf_connector_version_df.to_sql(
                'driver_versions', engine, index=False, method=make_pd_writer(quote_identifiers=False)))

    Args:
        quote_identifiers: if True (default), the pd_writer will pass quote identifiers to Snowflake.
            If False, the created pd_writer will not quote identifiers (and typically coerced to uppercase by Snowflake)
    """
    return partial(pd_writer, quote_identifiers=quote_identifiers)


def pd_writer(
    table: pandas.io.sql.SQLTable,
    conn: sqlalchemy.engine.Engine | sqlalchemy.engine.Connection,
    keys: Iterable,
    data_iter: Iterable,
    quote_identifiers: bool = True,
) -> None:
    """This is a wrapper on top of write_pandas to make it compatible with to_sql method in pandas.

        Example usage:
            import pandas as pd
            from snowflake.connector.pandas_tools import pd_writer

            sf_connector_version_df = pd.DataFrame([('snowflake-connector-python', '1.0')], columns=['NAME', 'NEWEST_VERSION'])
            sf_connector_version_df.to_sql('driver_versions', engine, index=False, method=pd_writer)

            # to use quote_identifiers=False, see `make_pd_writer`

    Args:
        table: Pandas package's table object.
        conn: SQLAlchemy engine object to talk to Snowflake.
        keys: Column names that we are trying to insert.
        data_iter: Iterator over the rows.
        quote_identifiers: if True (default), quote identifiers passed to Snowflake. If False, identifiers are not
            quoted (and typically coerced to uppercase by Snowflake)
    """
    sf_connection = conn.connection.connection
    df = pandas.DataFrame(data_iter, columns=keys)
    write_pandas(
        conn=sf_connection,
        df=df,
        # Note: Our sqlalchemy connector creates tables case insensitively
        table_name=table.name.upper(),
        schema=table.schema,
        quote_identifiers=quote_identifiers,
    )
