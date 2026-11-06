-- Run with sqlcmd as an Entra administrator, targeting the existing application database.
SET XACT_ABORT ON;
BEGIN TRANSACTION;

IF DB_NAME() <> N'$(DatabaseName)' OR DB_NAME() IN (N'master', N'model', N'msdb', N'tempdb')
    THROW 50000, 'Connect to the selected existing application database.', 1;

-- Service-principal SIDs use the application/client ID, not the directory object ID.
DECLARE @sid varbinary(16) = CONVERT(binary(16), CONVERT(uniqueidentifier, '$(ManagedIdentityClientId)'));
DECLARE @existingSid varbinary(85) = (SELECT sid FROM sys.database_principals WHERE name = N'pisign-app');
DECLARE @schemaOwner int = (SELECT principal_id FROM sys.schemas WHERE name = N'pisign');

IF @existingSid IS NULL
BEGIN
    IF @schemaOwner IS NOT NULL
        THROW 50000, 'The pisign schema already exists without the PiSign user. Refusing to adopt it.', 1;
    DECLARE @createUser nvarchar(max) = N'CREATE USER [pisign-app] WITH SID = '
        + CONVERT(nvarchar(34), @sid, 1) + N', TYPE = E';
    EXEC sp_executesql @createUser;
    EXEC(N'CREATE SCHEMA [pisign] AUTHORIZATION [pisign-app]');
    ALTER USER [pisign-app] WITH DEFAULT_SCHEMA = [pisign];
END
ELSE
BEGIN
    IF @existingSid <> @sid OR @schemaOwner <> DATABASE_PRINCIPAL_ID(N'pisign-app') OR @schemaOwner IS NULL
        THROW 50000, 'The existing PiSign user/schema belongs to another identity or is incomplete.', 1;
    IF (SELECT default_schema_name FROM sys.database_principals WHERE name = N'pisign-app') <> N'pisign'
        THROW 50000, 'The PiSign user must have default schema pisign.', 1;
END

-- Do not reuse an identity that already has database-wide roles or explicit permissions.
IF EXISTS (
    SELECT 1 FROM sys.database_role_members
    WHERE member_principal_id = DATABASE_PRINCIPAL_ID(N'pisign-app')
)
    THROW 50000, 'Remove existing database-wide role memberships before using this identity for PiSign.', 1;

IF EXISTS (
    SELECT 1 FROM sys.database_permissions
    WHERE grantee_principal_id = DATABASE_PRINCIPAL_ID(N'pisign-app')
      AND state IN ('G', 'W')
      AND (
          (class = 0 AND permission_name NOT IN (N'CONNECT', N'CREATE TABLE'))
          OR (class = 3 AND major_id <> SCHEMA_ID(N'pisign'))
          OR class NOT IN (0, 3)
      )
)
    THROW 50000, 'The PiSign identity already has permissions outside its schema. Refusing to reuse it.', 1;

GRANT CREATE TABLE TO [pisign-app];
GRANT SELECT, INSERT, UPDATE, DELETE, ALTER, REFERENCES, VIEW DEFINITION ON SCHEMA::[pisign] TO [pisign-app];
-- Block fallback to dbo and inherited public grants on other application schemas.
DECLARE @deny nvarchar(max);
SELECT @deny = STRING_AGG(
    CONVERT(nvarchar(max), N'DENY CONTROL, SELECT, INSERT, UPDATE, DELETE, ALTER, REFERENCES, EXECUTE, VIEW DEFINITION ON SCHEMA::'
        + QUOTENAME(name) + N' TO [pisign-app];'),
    NCHAR(10)
)
FROM sys.schemas
WHERE name NOT IN (N'pisign', N'sys', N'INFORMATION_SCHEMA');
EXEC sp_executesql @deny;

COMMIT TRANSACTION;
