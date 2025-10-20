"""Model monitoring module for Marvel characters."""

from databricks.sdk import WorkspaceClient
from databricks.sdk.errors import NotFound
from databricks.sdk.service.catalog import (
    MonitorInferenceLog,
    MonitorInferenceLogProblemType,
)
from loguru import logger
from pyspark.sql import SparkSession
from pyspark.sql import functions as F
from pyspark.sql.types import ArrayType, DoubleType, IntegerType, StringType, StructField, StructType

from marvel_characters.config import ProjectConfig


def create_or_refresh_monitoring(config: ProjectConfig, spark: SparkSession, workspace: WorkspaceClient) -> None:
    """Create or refresh a monitoring table for Marvel character model serving data.

    This function processes the inference data from a Delta table,
    parses the request and response JSON fields, writes the resulting DataFrame to a Delta table for monitoring purposes.

    :param config: Configuration object containing catalog and schema names.
    :param spark: Spark session used for executing SQL queries and transformations.
    :param workspace: Workspace object used for managing quality monitors.
    """
    # Check if custom_model_payload table exists and has data
    inf_table = spark.sql(f"SELECT * FROM {config.catalog_name}.{config.schema_name}.`custom_model_payload`")
    inf_count = inf_table.count()
    logger.info(f"Found {inf_count} records in custom_model_payload table")

    if inf_count == 0:
        logger.warning("No records found in custom_model_payload table. Monitoring table will be empty.")
        return

    request_schema = StructType(
        [
            StructField(
                "dataframe_records",
                ArrayType(
                    StructType(
                        [
                            StructField("Height", DoubleType(), True),
                            StructField("Weight", DoubleType(), True),
                            StructField("Universe", StringType(), True),
                            StructField("Identity", StringType(), True),
                            StructField("Gender", StringType(), True),
                            StructField("Marital_Status", StringType(), True),
                            StructField("Teams", StringType(), True),
                            StructField("Origin", StringType(), True),
                            StructField("Magic", StringType(), True),
                            StructField("Mutant", StringType(), True),
                        ]
                    )
                ),
                True,
            )
        ]
    )

    response_schema = StructType(
        [
            StructField("predictions", ArrayType(IntegerType()), True),
            StructField(
                "databricks_output",
                StructType(
                    [StructField("trace", StringType(), True), StructField("databricks_request_id", StringType(), True)]
                ),
                True,
            ),
        ]
    )

    inf_table_parsed = inf_table.withColumn("parsed_request", F.from_json(F.col("request"), request_schema))

    inf_table_parsed = inf_table_parsed.withColumn("parsed_response", F.from_json(F.col("response"), response_schema))

    df_exploded = inf_table_parsed.withColumn("record", F.explode(F.col("parsed_request.dataframe_records")))

    df_final = df_exploded.withColumn("timestamp_ms", (F.col("request_time").cast("long") * 1000)).select(
        F.col("request_time").alias("timestamp"),  # Use request_time as the timestamp
        F.col("timestamp_ms"),  # Select the newly created timestamp_ms column
        "databricks_request_id",
        "execution_duration_ms",
        F.col("record.Height").alias("Height"),
        F.col("record.Weight").alias("Weight"),
        F.col("record.Universe").alias("Universe"),
        F.col("record.Identity").alias("Identity"),
        F.col("record.Gender").alias("Gender"),
        F.col("record.Marital_Status").alias("Marital_Status"),
        F.col("record.Teams").alias("Teams"),
        F.col("record.Origin").alias("Origin"),
        F.col("record.Magic").alias("Magic"),
        F.col("record.Mutant").alias("Mutant"),
        F.col("parsed_response.predictions")[0].alias("prediction"),
        F.lit("marvel-characters-model-fe").alias("model_name"),
    )

    # Log counts at each step to diagnose where data might be getting filtered out
    logger.info(f"Records in df_final: {df_final.count()}")

    df_final_with_status = df_final.withColumn("prediction", F.col("prediction").cast("int"))

    # Make dropna optional if we're losing all data
    df_with_valid_values = df_final_with_status.dropna(subset=["prediction"])
    valid_count = df_with_valid_values.count()
    logger.info(f"Records with valid prediction values: {valid_count}")

    # If we lost all data after dropna, use the data before dropna
    if valid_count > 0:
        df_final_with_status = df_with_valid_values
        logger.info("Using records with valid prediction values")
    else:
        logger.warning("All records have null prediction values. Using records with potential nulls.")

    # Ensure Height and Weight are properly cast to double
    df_final_with_status = df_final_with_status.withColumn("Height", F.col("Height").cast("double"))
    df_final_with_status = df_final_with_status.withColumn("Weight", F.col("Weight").cast("double"))

    # Log the final count before writing
    final_count = df_final_with_status.count()
    logger.info(f"Final record count before writing to monitoring table: {final_count}")

    # Write to the monitoring table
    df_final_with_status.write.format("delta").mode("append").saveAsTable(
        f"{config.catalog_name}.{config.schema_name}.model_monitoring"
    )

    # Verify data was written
    written_count = spark.table(f"{config.catalog_name}.{config.schema_name}.model_monitoring").count()
    logger.info(f"Records in monitoring table after write: {written_count}")

    try:
        workspace.quality_monitors.get(f"{config.catalog_name}.{config.schema_name}.model_monitoring")
        workspace.quality_monitors.run_refresh(
            table_name=f"{config.catalog_name}.{config.schema_name}.model_monitoring"
        )
        logger.info("Lakehouse monitoring table exist, refreshing.")
    except NotFound:
        create_monitoring_table(config=config, spark=spark, workspace=workspace)
        logger.info("Lakehouse monitoring table is created.")


def create_monitoring_table(config: ProjectConfig, spark: SparkSession, workspace: WorkspaceClient) -> None:
    """Create a new monitoring table for Marvel character model monitoring.

    This function sets up a monitoring table using the provided configuration,
    SparkSession, and workspace. It also enables Change Data Feed for the table.

    :param config: Configuration object containing catalog and schema names
    :param spark: SparkSession object for executing SQL commands
    :param workspace: Workspace object for creating quality monitors
    """
    logger.info("Creating new monitoring table..")

    monitoring_table = f"{config.catalog_name}.{config.schema_name}.model_monitoring"

    workspace.quality_monitors.create(
        table_name=monitoring_table,
        assets_dir=f"/Workspace/Shared/lakehouse_monitoring/{monitoring_table}",
        output_schema_name=f"{config.catalog_name}.{config.schema_name}",
        inference_log=MonitorInferenceLog(
            problem_type=MonitorInferenceLogProblemType.PROBLEM_TYPE_CLASSIFICATION,
            prediction_col="prediction",
            timestamp_col="timestamp",
            granularities=["30 minutes"],
            model_id_col="model_name",
        ),
    )

    # Important to update monitoring
    spark.sql(f"ALTER TABLE {monitoring_table} SET TBLPROPERTIES (delta.enableChangeDataFeed = true);")

    logger.info("Lakehouse monitoring table is created.")
