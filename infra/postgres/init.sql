CREATE DATABASE support_test;
-- Langfuse (compose profile `langfuse`); langfuse-db-init creates it on volumes made before it existed.
CREATE DATABASE langfuse;
\connect support
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
\connect support_test
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
