-- Create non-superuser app role for RLS enforcement.
-- Only runs on first database initialization (docker-entrypoint-initdb.d).
DO $$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'angi_app') THEN
    CREATE ROLE angi_app LOGIN PASSWORD 'angi_app';
  END IF;
END
$$;

GRANT CONNECT ON DATABASE angi_lister TO angi_app;
GRANT USAGE ON SCHEMA public TO angi_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO angi_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO angi_app;
