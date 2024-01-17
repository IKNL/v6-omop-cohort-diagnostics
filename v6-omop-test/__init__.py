"""
This file contains all algorithm pieces that are executed on the nodes.
It is important to note that the main method is executed on a node, just
like any other method.

The results in a return statement are sent to the central vantage6 server after
encryption (if that is enabled for the collaboration).
"""
import pandas as pd

from vantage6.algorithm.tools.util import info
from vantage6.algorithm.tools.decorators import (
    algorithm_client,
    AlgorithmClient,
    database_connection,
    metadata,
    RunMetaData,
    OHDSIMetaData,
)

from ohdsi import circe
from ohdsi import cohort_generator
from ohdsi import common as ohdsi_common
from ohdsi import feature_extraction
from ohdsi import cohort_diagnostics as ohdsi_cohort_diagnostics

from rpy2.robjects import RS4


@algorithm_client
def central(
    client: AlgorithmClient,
    cohort_definitions: dict,
    cohort_names: list[str],
    temporal_covariate_settings: dict,
    diagnostics_settings: dict,
    organizations_to_include="ALL",
) -> list[pd.DataFrame]:
    """
    Executes the central algorithm on the specified client and returns the results.

    Parameters
    ----------
    client : AlgorithmClient
        Interface to the central server. This is supplied by the wrapper.
    cohort_definitions : dict
        A dictionary containing the cohort definitions from ATLAS.
    cohort_names : list[str]
        A list of cohort names.
    temporal_covariate_settings : dict
        A dictionary containing the temporal covariate settings.
    diagnostics_settings : dict
        A dictionary containing the diagnostics settings.
    organizations_to_include : str, optional
        The organizations to include. Defaults to 'ALL'.

    Returns
    -------
    list[pd.DataFrame]
        A list of pandas DataFrames containing the results.
    """
    info("Collecting participating organizations")
    # obtain organizations for which to run the algorithm
    organizations = client.organization.list()
    ids = [org["id"] for org in organizations]
    if organizations_to_include != "ALL":
        # check that organizations_to_include is a subset of ids, so we can return
        # a nice error message. The server can also return an error, but this is
        # more user friendly.
        if not set(organizations_to_include).issubset(set(ids)):
            return {
                "msg": "You specified an organization that is not part of the "
                "collaboration"
            }
        ids = organizations_to_include

    # This requests the cohort diagnostics to be computed on all nodes
    info("Requesting partial computation")
    task = client.task.create(
        input_={
            "method": "cohort_diagnostics",
            "kwargs": {
                "cohort_definitions": cohort_definitions,
                "cohort_names": cohort_names,
                "temporal_covariate_settings": temporal_covariate_settings,
                "diagnostics_settings": diagnostics_settings,
            },
        },
        organizations=ids,
    )
    info(f'Task assigned, id: {task.get("id")}')

    # This function is blocking until the results from all nodes are in
    info("Waiting for results")
    all_results = client.wait_for_results(task_id=task["id"])

    info("Results received, sending them back to server")
    return all_results


@metadata
@database_connection(types=["OMOP"], include_metadata=True)
def cohort_diagnostics(
    connection: RS4,
    meta_omop: OHDSIMetaData,
    meta_run: RunMetaData,
    cohort_definitions: dict,
    cohort_names: list[str],
    temporal_covariate_settings: dict,
    diagnostics_settings: dict,
) -> pd.DataFrame:
    """Computes the OHDSI cohort diagnostics."""

    # Generate unique cohort ids, based on the task id and the number of files.
    # The first six digits are the task id, the last three digits are the index
    # of the file.
    n = len(cohort_definitions)
    cohort_ids = [
        float(f"{meta_run.node_id}{meta_run.task_id:04d}{i:03d}") for i in range(0, n)
    ]
    info(f"cohort ids: {cohort_ids}")

    cohort_definition_set = pd.DataFrame(
        {
            "cohortId": cohort_ids,
            "cohortName": cohort_names,
            "json": cohort_definitions,
            "sql": [_create_cohort_query(cohort) for cohort in cohort_definitions],
            "logicDescription": [None] * n,
            "generateStats": [True] * n,
        }
    )
    info(f"Generated {n} cohort definitions")

    # Generate the table names for the cohort tables
    cohort_table = f"cohort_{meta_run.task_id}_{meta_run.node_id}"
    cohort_table_names = cohort_generator.get_cohort_table_names(cohort_table)
    info(f"Cohort table name: {cohort_table}")
    info(f"Tables: {cohort_table_names}")

    # Create the tables in the database
    info(f"OMOP results schema: {meta_omop.results_schema}")
    cohort_generator.create_cohort_tables(
        connection=connection,
        cohort_database_schema=meta_omop.results_schema,
        cohort_table_names=cohort_table_names,
    )
    info("Created cohort tables")

    # Generate the cohort set
    cohort_definition_set = ohdsi_common.convert_to_r(cohort_definition_set)
    cohort_generator.generate_cohort_set(
        connection=connection,
        cdm_database_schema=meta_omop.cdm_schema,
        cohort_database_schema=meta_omop.results_schema,
        cohort_table_names=cohort_table_names,
        cohort_definition_set=cohort_definition_set,
    )
    info("Generated cohort set")

    temporal_covariate_settings = feature_extraction.create_temporal_covariate_settings(
        **temporal_covariate_settings
    )
    info("Created temporal covariate settings")

    ohdsi_cohort_diagnostics.execute_diagnostics(
        cohort_definition_set=cohort_definition_set,
        export_folder=str(meta_omop.export_folder / "exports"),
        database_id=meta_run.task_id,
        database_name=f"{meta_run.task_id:06d}",
        database_description="todo",
        cohort_database_schema=meta_omop.results_schema,
        connection=connection,
        cdm_database_schema=meta_omop.cdm_schema,
        cohort_table=cohort_table,
        cohort_table_names=cohort_table_names,
        vocabulary_database_schema=meta_omop.cdm_schema,
        cohort_ids=None,
        cdm_version=5,
        temporal_covariate_settings=temporal_covariate_settings,
        **diagnostics_settings
        # min_cell_count=min_cell_count,
        # incremental=False, #default was True
        # incremental_folder=my_params['incremental_folder']
    )
    info("Executed diagnostics")

    # Read back the CSV file with the results
    df = pd.read_csv(meta_omop.export_folder / "exports" / "incidence_rate.csv")

    return df.to_json()


def _create_cohort_query(cohort_definition: dict) -> str:
    """
    Creates a cohort query from a cohort definition in JSON format.

    Parameters
    ----------
    cohort_definition: dict
        The cohort definition in JSON format, for example created from ATLAS.

    Returns
    -------
    str
        The cohort query.
    """
    cohort_expression = circe.cohort_expression_from_json(cohort_definition)
    options = circe.create_generate_options(generate_stats=True)
    return circe.build_cohort_query(cohort_expression, options)[0]
