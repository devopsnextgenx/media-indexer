-- Master file registry (one row per physical file on disk)
CREATE TABLE files (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,   -- or SERIAL/UUID in Postgres
    full_path     TEXT NOT NULL UNIQUE,                 -- absolute path, source of truth
    folder_path   TEXT NOT NULL,                         -- derived, indexed for "same folder" checks
    filename      TEXT NOT NULL,
    file_size     BIGINT,
    file_hash     TEXT,                                  -- exact-copy hash (sha256 of bytes)
    audio_fingerprint TEXT,                               -- e.g. chromaprint/acoustid, for near-dupe matching
    duration_secs REAL,
    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_files_folder ON files(folder_path);
CREATE INDEX idx_files_hash ON files(file_hash);
CREATE INDEX idx_files_fingerprint ON files(audio_fingerprint);

-- One row per detected duplicate SET (the "group")
CREATE TABLE duplicate_groups (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,  -- this is your group_id
    match_basis     TEXT,        -- 'exact_hash' | 'audio_fingerprint' | 'filename+size' | 'manual'
    representative_file_id INTEGER REFERENCES files(id), -- optional: the "keeper" once resolved
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    resolved_at     TIMESTAMP,   -- set when all members are confirmed/not_duplicate
    notes           TEXT
);

-- Membership of a file within a duplicate group, with its own status
CREATE TABLE duplicate_group_members (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    group_id      INTEGER NOT NULL REFERENCES duplicate_groups(id) ON DELETE CASCADE,
    file_id       INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    status        TEXT NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending', 'confirmed', 'not_duplicate')),
    similarity_score REAL,       -- optional confidence value from the matcher
    detected_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    reviewed_at   TIMESTAMP,     -- set when status moves off 'pending'
    reviewed_by   TEXT,          -- optional, if this is ever multi-user
    UNIQUE(group_id, file_id)
);

CREATE INDEX idx_dupmembers_group ON duplicate_group_members(group_id);
CREATE INDEX idx_dupmembers_file ON duplicate_group_members(file_id);
CREATE INDEX idx_dupmembers_status ON duplicate_group_members(status);