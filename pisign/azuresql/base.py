import logging

from azure.core.exceptions import AzureError
from azure.identity import ManagedIdentityCredential
from django.db import OperationalError
from mssql.base import DatabaseWrapper as MSSQLDatabaseWrapper
from mssql.schema import DatabaseSchemaEditor as MSSQLSchemaEditor

logger = logging.getLogger(__name__)


class DatabaseSchemaEditor(MSSQLSchemaEditor):
    # Preserve case-sensitive strings without changing the shared database collation.
    def _iter_column_sql(self, column_db_type, params, model, field, field_db_params, include_default):
        if column_db_type.startswith("nvarchar"):
            field_db_params = {**field_db_params, "collation": "Latin1_General_100_BIN2"}
        yield from super()._iter_column_sql(
            column_db_type, params, model, field, field_db_params, include_default,
        )

    def _alter_column_type_sql(self, model, old_field, new_field, new_type, old_collation, new_collation):
        if new_type.startswith("nvarchar"):
            new_collation = "Latin1_General_100_BIN2"
        return super()._alter_column_type_sql(
            model, old_field, new_field, new_type, old_collation, new_collation,
        )


class DatabaseWrapper(MSSQLDatabaseWrapper):
    SchemaEditorClass = DatabaseSchemaEditor

    def get_new_connection(self, conn_params):
        client_id = conn_params["OPTIONS"]["managed_identity_client_id"]
        try:
            with ManagedIdentityCredential(client_id=client_id) as credential:
                token = credential.get_token("https://database.windows.net/.default").token
        except AzureError:
            logger.warning("Azure SQL managed identity authentication failed.")
            raise OperationalError("Azure SQL managed identity authentication failed.") from None
        # Obtain a valid token for every new connection, not once at process startup.
        return super().get_new_connection({**conn_params, "TOKEN": token})

    def init_connection_state(self):
        super().init_connection_state()
        with self.connection.cursor() as cursor:
            cursor.execute("SELECT SCHEMA_NAME(), USER_NAME()")
            identity = cursor.fetchone()
        if identity is None or tuple(identity) != ("pisign", "pisign-app"):
            logger.warning("Azure SQL connection rejected: PiSign user/schema isolation is not configured.")
            self.close()
            raise OperationalError("Azure SQL requires the restricted pisign-app user with default schema pisign.")
