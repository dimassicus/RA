CREATE TABLE IF NOT EXISTS companies (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name TEXT NOT NULL,
    country_registration TEXT NOT NULL,
    registration_number TEXT NOT NULL DEFAULT '',
    key_owners TEXT NOT NULL DEFAULT '',
    top_management TEXT NOT NULL DEFAULT '',
    primary_countries TEXT NOT NULL DEFAULT '',
    affiliates TEXT NOT NULL DEFAULT '',
    primary_industries TEXT NOT NULL DEFAULT '',
    comment TEXT NOT NULL DEFAULT '',
    active BOOLEAN NOT NULL DEFAULT TRUE,
    workbook_row INTEGER,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT company_name_not_blank CHECK (length(btrim(name)) > 0),
    CONSTRAINT company_country_not_blank CHECK (length(btrim(country_registration)) > 0)
);

CREATE INDEX IF NOT EXISTS companies_active_name_idx
    ON companies (active, lower(name), lower(country_registration));
CREATE UNIQUE INDEX IF NOT EXISTS companies_registration_idx
    ON companies (lower(country_registration), registration_number)
    WHERE registration_number <> '';

CREATE TABLE IF NOT EXISTS company_identity_evidence (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    company_id BIGINT NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
    identifier_type TEXT NOT NULL,
    identifier_value TEXT NOT NULL,
    source_name TEXT NOT NULL,
    source_url TEXT NOT NULL,
    checked_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (company_id, identifier_type, identifier_value, source_url)
);

CREATE TABLE IF NOT EXISTS search_runs (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    scope TEXT NOT NULL CHECK (scope IN ('all', 'selected', 'one_off')),
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'running', 'completed', 'partial', 'failed')),
    subjects JSONB NOT NULL,
    total_companies INTEGER NOT NULL,
    completed_companies INTEGER NOT NULL DEFAULT 0,
    result_count INTEGER NOT NULL DEFAULT 0,
    error_count INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS search_results (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id BIGINT NOT NULL REFERENCES search_runs(id) ON DELETE CASCADE,
    company_id BIGINT REFERENCES companies(id) ON DELETE SET NULL,
    subject_key TEXT NOT NULL,
    subject_name TEXT NOT NULL,
    title TEXT NOT NULL,
    url TEXT NOT NULL,
    publisher TEXT NOT NULL DEFAULT '',
    source_kind TEXT NOT NULL DEFAULT 'news',
    published_at TIMESTAMPTZ,
    query TEXT NOT NULL,
    language TEXT NOT NULL,
    title_en TEXT NOT NULL DEFAULT '',
    publisher_en TEXT NOT NULL DEFAULT '',
    translation_provider TEXT NOT NULL DEFAULT '',
    signal_terms TEXT[] NOT NULL DEFAULT '{}',
    seen_before BOOLEAN NOT NULL DEFAULT FALSE,
    report_selected BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (run_id, subject_key, url)
);

CREATE INDEX IF NOT EXISTS search_results_run_date_idx
    ON search_results (run_id, published_at DESC);
CREATE INDEX IF NOT EXISTS search_results_history_idx
    ON search_results (subject_key, url);
ALTER TABLE search_results ADD COLUMN IF NOT EXISTS source_kind TEXT NOT NULL DEFAULT 'news';
ALTER TABLE search_results ADD COLUMN IF NOT EXISTS seen_before BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE search_results ADD COLUMN IF NOT EXISTS title_en TEXT NOT NULL DEFAULT '';
ALTER TABLE search_results ADD COLUMN IF NOT EXISTS publisher_en TEXT NOT NULL DEFAULT '';
ALTER TABLE search_results ADD COLUMN IF NOT EXISTS translation_provider TEXT NOT NULL DEFAULT '';
ALTER TABLE search_results ADD COLUMN IF NOT EXISTS report_selected BOOLEAN NOT NULL DEFAULT FALSE;

CREATE TABLE IF NOT EXISTS search_errors (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id BIGINT NOT NULL REFERENCES search_runs(id) ON DELETE CASCADE,
    subject_name TEXT NOT NULL,
    message TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
