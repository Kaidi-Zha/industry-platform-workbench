-- 工作台 v1 · 与既有运行库共存。索引及查重记录只保存元数据。
CREATE TABLE IF NOT EXISTS domains (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL COLLATE NOCASE UNIQUE CHECK (length(trim(name)) BETWEEN 1 AND 100),
    export_override_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(export_override_json) AND json_type(export_override_json) = 'object'),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

CREATE TABLE IF NOT EXISTS process_nodes (
    domain_id INTEGER NOT NULL REFERENCES domains(id),
    id TEXT NOT NULL,
    position INTEGER NOT NULL CHECK (position >= 1),
    title TEXT NOT NULL,
    occupations_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(occupations_json) AND json_type(occupations_json) = 'array'),
    flow_group TEXT NOT NULL DEFAULT '',
    lifecycle_stages_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(lifecycle_stages_json) AND json_type(lifecycle_stages_json) = 'array'),
    PRIMARY KEY (domain_id, id),
    UNIQUE (domain_id, position)
);

CREATE TABLE IF NOT EXISTS process_edges (
    domain_id INTEGER NOT NULL,
    upstream_id TEXT NOT NULL,
    downstream_id TEXT NOT NULL CHECK (upstream_id <> downstream_id),
    PRIMARY KEY (domain_id, upstream_id, downstream_id),
    FOREIGN KEY (domain_id, upstream_id) REFERENCES process_nodes(domain_id, id),
    FOREIGN KEY (domain_id, downstream_id) REFERENCES process_nodes(domain_id, id)
);

CREATE TABLE IF NOT EXISTS process_node_links (
    domain_id INTEGER NOT NULL,
    node_id TEXT NOT NULL,
    canonical_id TEXT NOT NULL CHECK (node_id <> canonical_id),
    reason TEXT NOT NULL CHECK (length(trim(reason)) > 0),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    PRIMARY KEY (domain_id, node_id),
    FOREIGN KEY (domain_id, node_id) REFERENCES process_nodes(domain_id, id),
    FOREIGN KEY (domain_id, canonical_id) REFERENCES process_nodes(domain_id, id)
);

CREATE TABLE IF NOT EXISTS process_node_link_audit (
    sequence INTEGER PRIMARY KEY,
    domain_id INTEGER NOT NULL REFERENCES domains(id),
    node_id TEXT NOT NULL,
    canonical_id TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('link', 'unlink')),
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS crawl_channels (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL
);

INSERT OR IGNORE INTO crawl_channels VALUES
    ('C1_STANDARD_ORG', '标准组织与法规库'),
    ('C2_OEM_FAB_SPEC', '头部制造／代工企业工艺手册'),
    ('C3_TOOL_FORMAT_DOC', '主流工程软件与格式官方文档'),
    ('C4_EQUIP_MAT_VENDOR', '设备与原材料原厂技术资料'),
    ('C5_ASSOC_WHITEPAPER', '行业协会与权威白皮书／研究报告'),
    ('C6_OPEN_ENG_REPO', '开源工程项目与基准测试样板'),
    ('C7_FAILURE_CASE', '工程失效复盘与真实工单／质量案例');

CREATE TABLE IF NOT EXISTS reference_authority_tiers (id TEXT PRIMARY KEY);
INSERT OR IGNORE INTO reference_authority_tiers VALUES
    ('T1_STANDARD'), ('T2_MANUFACTURER'), ('T3_TOOLCHAIN'), ('T4_CASE_SAMPLE');

CREATE TABLE IF NOT EXISTS reference_channel_tiers (
    channel TEXT PRIMARY KEY REFERENCES crawl_channels(id),
    tier TEXT NOT NULL REFERENCES reference_authority_tiers(id)
);
INSERT OR IGNORE INTO reference_channel_tiers VALUES
    ('C1_STANDARD_ORG', 'T1_STANDARD'), ('C2_OEM_FAB_SPEC', 'T2_MANUFACTURER'),
    ('C3_TOOL_FORMAT_DOC', 'T3_TOOLCHAIN'), ('C4_EQUIP_MAT_VENDOR', 'T2_MANUFACTURER'),
    ('C5_ASSOC_WHITEPAPER', 'T4_CASE_SAMPLE'), ('C6_OPEN_ENG_REPO', 'T4_CASE_SAMPLE'),
    ('C7_FAILURE_CASE', 'T4_CASE_SAMPLE');

CREATE TABLE IF NOT EXISTS frontier_rounds (
    domain_id INTEGER NOT NULL,
    node_id TEXT NOT NULL,
    number INTEGER NOT NULL CHECK (number >= 1),
    closed_at TEXT,
    PRIMARY KEY (domain_id, node_id, number),
    FOREIGN KEY (domain_id, node_id) REFERENCES process_nodes(domain_id, id)
);

CREATE TABLE IF NOT EXISTS raw_sources (
    domain_id INTEGER NOT NULL REFERENCES domains(id),
    id TEXT NOT NULL,
    canonical_url TEXT NOT NULL,
    version TEXT NOT NULL DEFAULT '',
    sha256 TEXT CHECK (length(sha256) = 64),
    status TEXT NOT NULL CHECK (status IN ('discovered', 'parsed', 'verified', 'rejected', 'blocked')),
    snapshot_uri TEXT,
    parsed_text_uri TEXT,
    retrieved_at TEXT,
    PRIMARY KEY (domain_id, id),
    UNIQUE (domain_id, canonical_url, version)
);

CREATE TABLE IF NOT EXISTS crawl_frontier (
    domain_id INTEGER NOT NULL,
    node_id TEXT NOT NULL,
    id TEXT NOT NULL,
    round_number INTEGER NOT NULL,
    channel_id TEXT NOT NULL REFERENCES crawl_channels(id),
    kind TEXT NOT NULL CHECK (kind IN ('query', 'url')),
    target TEXT NOT NULL,
    normalized_url TEXT,
    query_expansion TEXT CHECK (query_expansion IN ('seed', 'synonym', 'long_tail')),
    state TEXT NOT NULL CHECK (state IN ('queued', 'leased', 'retry', 'done', 'blocked')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    lease_token TEXT,
    lease_expires_at TEXT,
    next_attempt_at TEXT,
    last_error TEXT,
    source_id TEXT,
    PRIMARY KEY (domain_id, node_id, id),
    UNIQUE (domain_id, node_id, round_number, channel_id, kind, target),
    CHECK ((kind = 'query') = (query_expansion IS NOT NULL)),
    CHECK ((state = 'leased' AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL)
        OR (state <> 'leased' AND lease_token IS NULL AND lease_expires_at IS NULL)),
    FOREIGN KEY (domain_id, node_id, round_number) REFERENCES frontier_rounds(domain_id, node_id, number),
    FOREIGN KEY (domain_id, source_id) REFERENCES raw_sources(domain_id, id)
);
CREATE INDEX IF NOT EXISTS frontier_by_state ON crawl_frontier(state, next_attempt_at, lease_expires_at);

CREATE TABLE IF NOT EXISTS entity_fingerprints (
    domain_id INTEGER NOT NULL,
    node_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL CHECK (length(fingerprint) = 64),
    source_id TEXT NOT NULL,
    first_round INTEGER NOT NULL,
    PRIMARY KEY (domain_id, node_id, fingerprint),
    FOREIGN KEY (domain_id, source_id) REFERENCES raw_sources(domain_id, id),
    FOREIGN KEY (domain_id, node_id, first_round) REFERENCES frontier_rounds(domain_id, node_id, number)
);

CREATE TABLE IF NOT EXISTS frontier_candidates (
    domain_id INTEGER NOT NULL,
    node_id TEXT NOT NULL,
    round_number INTEGER NOT NULL,
    source_id TEXT NOT NULL,
    fingerprint TEXT CHECK (length(fingerprint) = 64),
    assessment TEXT NOT NULL CHECK (assessment IN ('pending', 'eligible', 'ineligible')),
    CHECK (assessment <> 'eligible' OR fingerprint IS NOT NULL),
    PRIMARY KEY (domain_id, node_id, round_number, source_id),
    FOREIGN KEY (domain_id, node_id, round_number) REFERENCES frontier_rounds(domain_id, node_id, number),
    FOREIGN KEY (domain_id, source_id) REFERENCES raw_sources(domain_id, id),
    FOREIGN KEY (domain_id, node_id, fingerprint) REFERENCES entity_fingerprints(domain_id, node_id, fingerprint)
);

CREATE VIEW IF NOT EXISTS frontier_round_metrics AS
    SELECT r.domain_id, r.node_id, r.number, r.closed_at,
           COUNT(CASE WHEN c.assessment IN ('eligible', 'ineligible') THEN 1 END) AS assessed,
           COUNT(DISTINCT CASE WHEN e.first_round = r.number THEN c.fingerprint END) AS novel,
           COUNT(CASE WHEN c.assessment = 'pending' THEN 1 END) AS pending
    FROM frontier_rounds r
    LEFT JOIN frontier_candidates c ON (c.domain_id, c.node_id, c.round_number) = (r.domain_id, r.node_id, r.number)
    LEFT JOIN entity_fingerprints e ON
        (e.domain_id, e.node_id, e.fingerprint) = (c.domain_id, c.node_id, c.fingerprint)
        AND c.assessment = 'eligible'
    GROUP BY r.domain_id, r.node_id, r.number;

CREATE TABLE IF NOT EXISTS verified_references (
    domain_id INTEGER NOT NULL,
    id TEXT NOT NULL,
    source_id TEXT NOT NULL,
    node_id TEXT,
    publisher TEXT NOT NULL CHECK (length(trim(publisher)) > 0),
    title TEXT NOT NULL CHECK (length(trim(title)) > 0),
    quality_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(quality_json) AND json_type(quality_json)='object'),
    authority TEXT NOT NULL CHECK (authority IN ('acceptance', 'standard', 'manufacturer', 'expert', 'research')),
    authority_tier TEXT REFERENCES reference_authority_tiers(id),
    source_channel TEXT REFERENCES crawl_channels(id),
    object_cluster TEXT,
    lifecycle_stages_json TEXT CHECK (lifecycle_stages_json IS NULL OR json_valid(lifecycle_stages_json)),
    locator TEXT NOT NULL CHECK (length(trim(locator)) > 0),
    finding TEXT NOT NULL CHECK (length(trim(finding)) > 0),
    attachment_uri TEXT,
    permission TEXT NOT NULL CHECK (permission IN ('stated', 'unknown', 'restricted')),
    verified_at TEXT NOT NULL,
    PRIMARY KEY (domain_id, id),
    FOREIGN KEY (domain_id, source_id) REFERENCES raw_sources(domain_id, id),
    FOREIGN KEY (domain_id, node_id) REFERENCES process_nodes(domain_id, id)
);

CREATE TABLE IF NOT EXISTS verified_reference_nodes (
    domain_id INTEGER NOT NULL,
    reference_id TEXT NOT NULL,
    node_id TEXT NOT NULL CHECK (node_id <> 'unassigned'),
    PRIMARY KEY (domain_id, reference_id, node_id),
    FOREIGN KEY (domain_id, reference_id) REFERENCES verified_references(domain_id, id) ON DELETE CASCADE,
    FOREIGN KEY (domain_id, node_id) REFERENCES process_nodes(domain_id, id)
);
CREATE INDEX IF NOT EXISTS reference_nodes_by_process ON verified_reference_nodes(domain_id,node_id,reference_id);

CREATE TABLE IF NOT EXISTS process_relations (
    domain_id INTEGER NOT NULL,
    node_id TEXT NOT NULL,
    id TEXT NOT NULL,
    sender TEXT NOT NULL,
    recipient TEXT NOT NULL,
    deliverable TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('actual_delivery', 'supplier_requirement', 'collaboration', 'demonstration', 'compatibility')),
    evidence_id TEXT,
    evidence_clause_id TEXT,
    evidence_url TEXT,
    evidence_locator TEXT,
    evidence_quote TEXT,
    state TEXT NOT NULL CHECK (state IN ('pending', 'verified')),
    CHECK (state <> 'verified' OR evidence_id IS NOT NULL),
    PRIMARY KEY (domain_id, node_id, id),
    FOREIGN KEY (domain_id, node_id) REFERENCES process_nodes(domain_id, id),
    FOREIGN KEY (domain_id, evidence_id) REFERENCES verified_references(domain_id, id)
);

CREATE TABLE IF NOT EXISTS process_participants (
    domain_id INTEGER NOT NULL,
    node_id TEXT NOT NULL,
    name TEXT NOT NULL COLLATE NOCASE,
    role TEXT NOT NULL CHECK (role IN ('upstream', 'manufacturer', 'downstream')),
    reference_id TEXT NOT NULL,
    PRIMARY KEY (domain_id, node_id, name, role),
    FOREIGN KEY (domain_id, node_id) REFERENCES process_nodes(domain_id, id) ON DELETE CASCADE,
    FOREIGN KEY (domain_id, reference_id) REFERENCES verified_references(domain_id, id)
);

CREATE TABLE IF NOT EXISTS process_coverage_targets (
    domain_id INTEGER NOT NULL,
    node_id TEXT NOT NULL,
    expected_sources INTEGER NOT NULL CHECK (expected_sources > 0),
    basis_reference_id TEXT NOT NULL,
    PRIMARY KEY (domain_id, node_id),
    FOREIGN KEY (domain_id, node_id) REFERENCES process_nodes(domain_id, id),
    FOREIGN KEY (domain_id, basis_reference_id) REFERENCES verified_references(domain_id, id)
);

CREATE TABLE IF NOT EXISTS benchmark_tasks (
    domain_id INTEGER NOT NULL,
    id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    title TEXT NOT NULL,
    quality_approved INTEGER NOT NULL DEFAULT 0 CHECK (quality_approved IN (0,1)),
    definition_json TEXT NOT NULL CHECK (json_valid(definition_json) AND json_type(definition_json) = 'object'),
    necessity INTEGER CHECK (necessity BETWEEN 1 AND 5),
    frequency_basis TEXT,
    expert_minutes INTEGER CHECK (expert_minutes >= 0),
    error_loss_basis TEXT,
    downstream_id TEXT,
    complexity TEXT CHECK (complexity IN ('L1', 'L2', 'L3', 'L4')),
    state TEXT NOT NULL CHECK (state IN ('draft', 'ready', 'archived')),
    PRIMARY KEY (domain_id, id),
    FOREIGN KEY (domain_id, node_id) REFERENCES process_nodes(domain_id, id),
    FOREIGN KEY (domain_id, downstream_id) REFERENCES process_nodes(domain_id, id)
);

CREATE TABLE IF NOT EXISTS task_references (
    domain_id INTEGER NOT NULL,
    task_id TEXT NOT NULL,
    reference_id TEXT NOT NULL,
    PRIMARY KEY (domain_id, task_id, reference_id),
    FOREIGN KEY (domain_id, task_id) REFERENCES benchmark_tasks(domain_id, id),
    FOREIGN KEY (domain_id, reference_id) REFERENCES verified_references(domain_id, id)
);

CREATE TABLE IF NOT EXISTS verifier_specs (
    domain_id INTEGER NOT NULL,
    task_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version >= 1),
    strategy TEXT NOT NULL CHECK (strategy IN ('acceptance_backwards', 'progressive')),
    image TEXT,
    layers_json TEXT NOT NULL CHECK (json_valid(layers_json) AND json_type(layers_json) = 'object'
        AND json_type(layers_json, '$.L1') IS 'array' AND json_type(layers_json, '$.L2') IS 'array'
        AND json_type(layers_json, '$.L3') IS 'array' AND json_type(layers_json, '$.L4') IS 'array'),
    self_tests_json TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(self_tests_json) AND json_type(self_tests_json) = 'array'),
    PRIMARY KEY (domain_id, task_id, version),
    FOREIGN KEY (domain_id, task_id) REFERENCES benchmark_tasks(domain_id, id)
);

CREATE TABLE IF NOT EXISTS verifier_samples (
    domain_id INTEGER NOT NULL REFERENCES domains(id) ON DELETE CASCADE,
    sha256 TEXT NOT NULL,
    content BLOB NOT NULL,
    PRIMARY KEY (domain_id, sha256)
);

CREATE TABLE IF NOT EXISTS eval_runs (
    domain_id INTEGER NOT NULL,
    id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    verifier_version INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('queued', 'running', 'completed', 'failed', 'cancelled')),
    model_matrix_json TEXT NOT NULL CHECK (json_valid(model_matrix_json) AND json_type(model_matrix_json) = 'array'),
    pass_at_k INTEGER NOT NULL CHECK (pass_at_k >= 1),
    results_json TEXT NOT NULL DEFAULT '{}' CHECK (json_valid(results_json) AND json_type(results_json) = 'object'),
    difficulty TEXT CHECK (difficulty IN ('easy', 'medium', 'hard', 'frontier')),
    confirmed_at TEXT NOT NULL,
    PRIMARY KEY (domain_id, id),
    FOREIGN KEY (domain_id, task_id, verifier_version) REFERENCES verifier_specs(domain_id, task_id, version)
);

CREATE TABLE IF NOT EXISTS global_settings (
    key TEXT PRIMARY KEY,
    value_json TEXT NOT NULL CHECK (json_valid(value_json))
);

CREATE TABLE IF NOT EXISTS harness_threads (
    domain_id INTEGER NOT NULL REFERENCES domains(id),
    thread_id TEXT NOT NULL,
    parent_thread_id TEXT,
    conversation_id TEXT REFERENCES runs(id),
    profile_id TEXT,
    role TEXT NOT NULL,
    workdir TEXT NOT NULL,
    sandbox_mode TEXT NOT NULL CHECK (sandbox_mode IN ('read-only', 'workspace-write', 'danger-full-access')),
    PRIMARY KEY (domain_id, thread_id),
    FOREIGN KEY (domain_id, parent_thread_id) REFERENCES harness_threads(domain_id, thread_id)
);

CREATE TABLE IF NOT EXISTS frontier_campaigns (
    domain_id INTEGER NOT NULL,
    node_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('running', 'blocked', 'exhausted')),
    PRIMARY KEY(domain_id, node_id),
    FOREIGN KEY(domain_id, node_id) REFERENCES process_nodes(domain_id, id)
);

CREATE TABLE IF NOT EXISTS frontier_reviews (
    domain_id INTEGER NOT NULL,
    node_id TEXT NOT NULL,
    round_number INTEGER NOT NULL,
    source_id TEXT NOT NULL,
    note TEXT NOT NULL,
    PRIMARY KEY(domain_id, node_id, round_number, source_id),
    FOREIGN KEY(domain_id, node_id, round_number, source_id)
        REFERENCES frontier_candidates(domain_id, node_id, round_number, source_id)
);

CREATE TABLE IF NOT EXISTS domain_network_approvals (
    domain_id INTEGER NOT NULL REFERENCES domains(id),
    destination TEXT NOT NULL,
    PRIMARY KEY(domain_id, destination)
);

CREATE TABLE IF NOT EXISTS domain_attachments (
    domain_id INTEGER NOT NULL REFERENCES domains(id),
    id TEXT NOT NULL,
    name TEXT NOT NULL,
    bytes INTEGER NOT NULL CHECK (bytes > 0),
    sha256 TEXT NOT NULL,
    PRIMARY KEY(domain_id, id)
);

CREATE TABLE IF NOT EXISTS process_node_evidence (
    domain_id INTEGER NOT NULL,
    node_id TEXT NOT NULL,
    reference_id TEXT NOT NULL REFERENCES evidence_references(id),
    PRIMARY KEY(domain_id,node_id,reference_id),
    FOREIGN KEY(domain_id,node_id) REFERENCES process_nodes(domain_id,id)
);
