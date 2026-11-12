CREATE TABLE IF NOT EXISTS companies (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name TEXT NOT NULL,
    short_name TEXT NOT NULL DEFAULT '',
    country_registration TEXT NOT NULL,
    registration_number TEXT NOT NULL DEFAULT '',
    tax_id TEXT NOT NULL DEFAULT '',
    key_owners TEXT NOT NULL DEFAULT '',
    top_management TEXT NOT NULL DEFAULT '',
    primary_countries TEXT NOT NULL DEFAULT '',
    affiliates TEXT NOT NULL DEFAULT '',
    primary_industries TEXT NOT NULL DEFAULT '',
    comment TEXT NOT NULL DEFAULT '',
    legal_address TEXT NOT NULL DEFAULT '',
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
ALTER TABLE companies ADD COLUMN IF NOT EXISTS short_name TEXT NOT NULL DEFAULT '';
ALTER TABLE companies ADD COLUMN IF NOT EXISTS legal_address TEXT NOT NULL DEFAULT '';
ALTER TABLE companies ADD COLUMN IF NOT EXISTS tax_id TEXT NOT NULL DEFAULT '';
ALTER TABLE companies ADD COLUMN IF NOT EXISTS disclosure_sources JSONB NOT NULL DEFAULT '{}'::jsonb;
CREATE UNIQUE INDEX IF NOT EXISTS companies_tax_id_idx
    ON companies (lower(country_registration), tax_id)
    WHERE tax_id <> '';

CREATE TABLE IF NOT EXISTS company_intake (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source TEXT NOT NULL,
    source_key TEXT NOT NULL UNIQUE,
    workbook_row INTEGER,
    draft_data JSONB NOT NULL,
    findings JSONB NOT NULL DEFAULT '{}'::jsonb,
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'researching', 'ready', 'error', 'saved', 'dismissed')),
    matched_company_id BIGINT REFERENCES companies(id) ON DELETE SET NULL,
    company_id BIGINT REFERENCES companies(id) ON DELETE SET NULL,
    research_error TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS company_intake_pending_idx ON company_intake (status, id DESC);

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

CREATE TABLE IF NOT EXISTS search_preferences (
    id BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (id),
    include_country BOOLEAN NOT NULL DEFAULT TRUE,
    include_industry BOOLEAN NOT NULL DEFAULT TRUE,
    adverse_only BOOLEAN NOT NULL DEFAULT FALSE
);

ALTER TABLE search_preferences ADD COLUMN IF NOT EXISTS use_perplexity BOOLEAN NOT NULL DEFAULT TRUE;
ALTER TABLE search_preferences ADD COLUMN IF NOT EXISTS use_google BOOLEAN NOT NULL DEFAULT TRUE;

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

ALTER TABLE search_runs ADD COLUMN IF NOT EXISTS candidates_in_window INTEGER;
ALTER TABLE search_runs ADD COLUMN IF NOT EXISTS monthly_backfill JSONB NOT NULL DEFAULT '{}';
ALTER TABLE search_runs ADD COLUMN IF NOT EXISTS retry_translations BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE search_runs ADD COLUMN IF NOT EXISTS window_start TIMESTAMPTZ;
ALTER TABLE search_runs ADD COLUMN IF NOT EXISTS window_end TIMESTAMPTZ;
ALTER TABLE search_runs ADD COLUMN IF NOT EXISTS search_settings JSONB NOT NULL DEFAULT '{}';

CREATE TABLE IF NOT EXISTS search_results (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id BIGINT NOT NULL REFERENCES search_runs(id) ON DELETE CASCADE,
    company_id BIGINT REFERENCES companies(id) ON DELETE SET NULL,
    subject_key TEXT NOT NULL,
    subject_name TEXT NOT NULL,
    title TEXT NOT NULL,
    url TEXT NOT NULL,
    publisher TEXT NOT NULL DEFAULT '',
    discovery_service TEXT NOT NULL DEFAULT '',
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

ALTER TABLE search_results ADD COLUMN IF NOT EXISTS discovery_summary TEXT NOT NULL DEFAULT '';
ALTER TABLE search_results ADD COLUMN IF NOT EXISTS exposure_paths TEXT[] NOT NULL DEFAULT '{}';

CREATE INDEX IF NOT EXISTS search_results_run_date_idx
    ON search_results (run_id, published_at DESC);
CREATE INDEX IF NOT EXISTS search_results_history_idx
    ON search_results (subject_key, url);
ALTER TABLE search_results ADD COLUMN IF NOT EXISTS source_kind TEXT NOT NULL DEFAULT 'news';
ALTER TABLE search_results ADD COLUMN IF NOT EXISTS discovery_service TEXT NOT NULL DEFAULT '';
ALTER TABLE search_results ADD COLUMN IF NOT EXISTS seen_before BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE search_results ADD COLUMN IF NOT EXISTS title_en TEXT NOT NULL DEFAULT '';
ALTER TABLE search_results ADD COLUMN IF NOT EXISTS publisher_en TEXT NOT NULL DEFAULT '';
ALTER TABLE search_results ADD COLUMN IF NOT EXISTS decision_followup_urls TEXT[] NOT NULL DEFAULT '{}';
ALTER TABLE search_results ADD COLUMN IF NOT EXISTS translation_provider TEXT NOT NULL DEFAULT '';
ALTER TABLE search_results ADD COLUMN IF NOT EXISTS report_selected BOOLEAN NOT NULL DEFAULT FALSE;

CREATE TABLE IF NOT EXISTS search_errors (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id BIGINT NOT NULL REFERENCES search_runs(id) ON DELETE CASCADE,
    subject_name TEXT NOT NULL,
    message TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS news_api_usage (
    provider TEXT NOT NULL,
    usage_date DATE NOT NULL,
    request_count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (provider, usage_date)
);

CREATE TABLE IF NOT EXISTS translation_attempts (
    factors BOOLEAN NOT NULL,
    row_id BIGINT NOT NULL,
    language TEXT NOT NULL,
    english TEXT NOT NULL,
    review JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (factors, row_id, language)
);

ALTER TABLE search_results ADD COLUMN IF NOT EXISTS score SMALLINT CONSTRAINT search_results_score_three_grades CHECK (score BETWEEN -1 AND 1);
ALTER TABLE search_results ADD COLUMN IF NOT EXISTS score_auto SMALLINT CONSTRAINT search_results_score_auto_three_grades CHECK (score_auto BETWEEN -1 AND 1);
ALTER TABLE search_results ADD COLUMN IF NOT EXISTS score_reason TEXT NOT NULL DEFAULT '';
ALTER TABLE search_results ADD COLUMN IF NOT EXISTS score_evidence JSONB NOT NULL DEFAULT '[]';
ALTER TABLE search_results ADD COLUMN IF NOT EXISTS score_status TEXT NOT NULL DEFAULT 'pending'
    CHECK (score_status IN ('pending', 'provisional', 'needs_review', 'reviewed'));
ALTER TABLE search_results ADD COLUMN IF NOT EXISTS score_included BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE search_results ADD COLUMN IF NOT EXISTS score_version TEXT NOT NULL DEFAULT '';
ALTER TABLE search_results ADD COLUMN IF NOT EXISTS score_input_hash TEXT NOT NULL DEFAULT '';
ALTER TABLE search_results ADD COLUMN IF NOT EXISTS score_updated_at TIMESTAMPTZ;

CREATE TABLE IF NOT EXISTS article_score_history (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    result_id BIGINT REFERENCES search_results(id) ON DELETE SET NULL,
    company_id BIGINT REFERENCES companies(id) ON DELETE SET NULL,
    url TEXT NOT NULL,
    previous JSONB NOT NULL,
    assessment JSONB NOT NULL,
    reason TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS company_weekly_scores (
    company_id BIGINT NOT NULL REFERENCES companies(id),
    week_start DATE NOT NULL,
    score SMALLINT CONSTRAINT company_weekly_scores_score_three_grades CHECK (score BETWEEN -1 AND 1),
    mean_score NUMERIC(4,2) CONSTRAINT company_weekly_scores_mean_score_three_grades CHECK (mean_score BETWEEN -1 AND 1),
    metrics JSONB NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (company_id, week_start)
);
CREATE TABLE IF NOT EXISTS weekly_score_history (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    company_id BIGINT NOT NULL REFERENCES companies(id),
    week_start DATE NOT NULL,
    snapshot JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS weekly_score_history_company_week_idx
    ON weekly_score_history (company_id, week_start, id DESC);

ALTER TABLE companies ADD COLUMN IF NOT EXISTS aliases TEXT NOT NULL DEFAULT '';
ALTER TABLE companies ADD COLUMN IF NOT EXISTS alias_notes TEXT NOT NULL DEFAULT '';

ALTER TABLE search_results ADD COLUMN IF NOT EXISTS verification JSONB NOT NULL DEFAULT '{"status":"pending"}';
ALTER TABLE search_results ADD COLUMN IF NOT EXISTS event_group TEXT NOT NULL DEFAULT '';
ALTER TABLE search_results ADD COLUMN IF NOT EXISTS event_group_manual TEXT NOT NULL DEFAULT '';
CREATE TABLE IF NOT EXISTS article_documents (
    url TEXT PRIMARY KEY,
    document JSONB NOT NULL,
    fetched_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS article_verification_history (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    result_id BIGINT REFERENCES search_results(id) ON DELETE SET NULL,
    previous JSONB NOT NULL,
    assessment JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS article_event_history (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    result_id BIGINT REFERENCES search_results(id) ON DELETE SET NULL,
    previous_group TEXT NOT NULL,
    new_group TEXT NOT NULL,
    reason TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS verification_jobs (
    result_id BIGINT PRIMARY KEY REFERENCES search_results(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','running','completed','failed')),
    error TEXT NOT NULL DEFAULT '',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
ALTER TABLE verification_jobs ADD COLUMN IF NOT EXISTS source_url TEXT NOT NULL DEFAULT '';

CREATE TABLE IF NOT EXISTS exposure_checks (
    run_id BIGINT NOT NULL REFERENCES search_runs(id) ON DELETE CASCADE,
    branch TEXT NOT NULL CHECK (branch IN ('country', 'sector')),
    topic TEXT NOT NULL,
    hits JSONB NOT NULL DEFAULT '[]',
    error TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (run_id, branch, topic)
);

ALTER TABLE search_results ADD COLUMN IF NOT EXISTS headline_review JSONB NOT NULL DEFAULT '{}'::jsonb;

CREATE TABLE IF NOT EXISTS monitor_keywords (
    id bigserial PRIMARY KEY,
    english text NOT NULL UNIQUE,
    seed_key text UNIQUE,
    direction text NOT NULL CHECK (direction IN ('adverse','favorable','neutral')),
    enabled boolean NOT NULL DEFAULT true,
    translations jsonb NOT NULL DEFAULT '{}'
);

-- Keywords and their related phrases share the keyword review audit ID namespace.
CREATE TABLE IF NOT EXISTS keyword_phrases (
    id bigint PRIMARY KEY DEFAULT nextval('monitor_keywords_id_seq'),
    family text NOT NULL,
    english text NOT NULL,
    translations jsonb NOT NULL DEFAULT '{}',
    UNIQUE (family, english)
);

CREATE TABLE IF NOT EXISTS governance_rosters (
    run_id BIGINT NOT NULL REFERENCES search_runs(id) ON DELETE CASCADE,
    subject_key TEXT NOT NULL,
    company_name TEXT NOT NULL,
    source_url TEXT NOT NULL,
    observed_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    source_label TEXT NOT NULL,
    members JSONB NOT NULL,
    PRIMARY KEY (run_id, subject_key, source_url)
);
CREATE INDEX IF NOT EXISTS governance_rosters_source_idx ON governance_rosters (source_url, observed_at DESC);

ALTER TABLE governance_rosters ADD COLUMN IF NOT EXISTS coverage_note TEXT NOT NULL DEFAULT '';
ALTER TABLE governance_rosters ADD COLUMN IF NOT EXISTS evidence_url TEXT NOT NULL DEFAULT '';

CREATE TABLE IF NOT EXISTS risk_factors (
    id bigserial PRIMARY KEY,
    english text NOT NULL,
    seed_key text UNIQUE,
    scope text NOT NULL CHECK (scope IN ('company', 'sector', 'country')),
    enabled boolean NOT NULL DEFAULT true,
    translations jsonb NOT NULL DEFAULT '{}'
);
CREATE UNIQUE INDEX IF NOT EXISTS risk_factors_english_idx ON risk_factors (lower(english));
ALTER TABLE risk_factors ADD COLUMN IF NOT EXISTS search_cues jsonb NOT NULL DEFAULT '{}';
ALTER TABLE search_results ADD COLUMN IF NOT EXISTS risk_factor_review jsonb NOT NULL DEFAULT '{}';

ALTER TABLE search_results ADD COLUMN IF NOT EXISTS event_draft JSONB NOT NULL DEFAULT '{}';
ALTER TABLE search_runs ADD COLUMN IF NOT EXISTS event_analysis_status TEXT NOT NULL DEFAULT 'pending';
ALTER TABLE search_runs ADD COLUMN IF NOT EXISTS selection_saved BOOLEAN NOT NULL DEFAULT FALSE;

-- Every failed search keeps its original company, edition and publication window.
CREATE TABLE IF NOT EXISTS rss_recovery (
    id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id BIGINT NOT NULL REFERENCES search_runs ON DELETE CASCADE,
    subject_key TEXT NOT NULL,
    request JSONB NOT NULL,
    fingerprint TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','recovered','unknown_edition')),
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt TIMESTAMPTZ NOT NULL DEFAULT now(),
    detail TEXT NOT NULL DEFAULT '',
    UNIQUE (run_id,subject_key,fingerprint)
);
ALTER TABLE rss_recovery ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT now();
CREATE INDEX IF NOT EXISTS rss_recovery_pending ON rss_recovery(next_attempt,id) WHERE status='pending';
CREATE TABLE IF NOT EXISTS rss_brave_attempts (
    run_id BIGINT NOT NULL REFERENCES search_runs ON DELETE CASCADE,
    subject_key TEXT NOT NULL,
    outcome TEXT NOT NULL DEFAULT 'requested',
    PRIMARY KEY(run_id,subject_key)
);
ALTER TABLE rss_brave_attempts ADD COLUMN IF NOT EXISTS staged JSONB;
ALTER TABLE search_errors ADD COLUMN IF NOT EXISTS rss_imported BOOLEAN NOT NULL DEFAULT FALSE;
CREATE TABLE IF NOT EXISTS rss_cooldown (
    id BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK(id),
    until_time TIMESTAMPTZ NOT NULL,
    delay_seconds INTEGER NOT NULL
);

-- Existing runs keep the shared evidence budget, including on retry.
ALTER TABLE search_runs ADD COLUMN IF NOT EXISTS per_document_limit BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE search_runs ALTER COLUMN per_document_limit SET DEFAULT TRUE;
ALTER TABLE search_runs ADD COLUMN IF NOT EXISTS filing_coverage JSONB NOT NULL DEFAULT '{}';
ALTER TABLE search_results ADD COLUMN IF NOT EXISTS filing_attachments JSONB NOT NULL DEFAULT '[]';

ALTER TABLE verification_jobs ADD COLUMN IF NOT EXISTS translation_checkpoint JSONB NOT NULL DEFAULT '{}';
ALTER TABLE search_runs ADD COLUMN IF NOT EXISTS completed_subject_keys TEXT[] NOT NULL DEFAULT '{}';

-- Display translations are shared by exact source wording. They never enter scoring or event grouping.
CREATE TABLE IF NOT EXISTS result_translations (
    key TEXT PRIMARY KEY, source TEXT NOT NULL, language TEXT NOT NULL,
    subject JSONB NOT NULL DEFAULT '{}', english TEXT NOT NULL DEFAULT '', russian TEXT NOT NULL DEFAULT '',
    retry_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS result_translations_pending ON result_translations(retry_at) WHERE english='' OR russian='';
