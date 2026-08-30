CREATE TABLE `download_tracker` (
  `entry` varchar(768) NOT NULL,
  `status` varchar(50) DEFAULT 'PENDING',
  `updated_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `title` varchar(255) DEFAULT NULL,
  PRIMARY KEY (`entry`),
  KEY `idx_status` (`status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci

CREATE TABLE `indexing_jobs` (
  `job_id` varchar(255) NOT NULL,
  `mount_name` varchar(255) NOT NULL,
  `status` varchar(50) DEFAULT 'PENDING',
  `created_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  `total_files` int DEFAULT '0',
  `processed_files` int DEFAULT '0',
  `added_files` int DEFAULT '0',
  `updated_files` int DEFAULT '0',
  `skipped_files` int DEFAULT '0',
  `failed_files` int DEFAULT '0',
  `cleaned_orphans` int DEFAULT '0',
  `eta_seconds` int DEFAULT '0',
  `error_message` text,
  PRIMARY KEY (`job_id`),
  KEY `idx_mount_status` (`mount_name`,`status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci

CREATE TABLE `duplicate_groups` (
  `group_id` varchar(80) NOT NULL,
  `title_key` varchar(512) DEFAULT NULL,
  `member_count` int DEFAULT NULL,
  `created_at` datetime DEFAULT NULL,
  `updated_at` datetime DEFAULT NULL,
  PRIMARY KEY (`group_id`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci

CREATE TABLE `duplicate_group_candidates` (
  `id` int NOT NULL AUTO_INCREMENT,
  `group_id` varchar(80) NOT NULL,
  `file_id` int NOT NULL,
  `full_path` varchar(1024) DEFAULT NULL,
  `title_score` decimal(5,1) DEFAULT NULL,
  `movie_score` decimal(5,1) DEFAULT NULL,
  `artist_score` decimal(5,1) DEFAULT NULL,
  `overall_score` decimal(5,1) DEFAULT NULL,
  `confidence` enum('HIGH','MEDIUM','LOW') DEFAULT 'LOW',
  `status` enum('PENDING','DUPLICATE','REJECTED') DEFAULT 'PENDING',
  `stats_json` json DEFAULT NULL,
  `created_at` datetime DEFAULT NULL,
  `updated_at` datetime DEFAULT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_group_file` (`group_id`,`file_id`),
  KEY `idx_status` (`status`),
  CONSTRAINT `fk_dgc_group` FOREIGN KEY (`group_id`) REFERENCES `duplicate_groups` (`group_id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci

CREATE TABLE `processed_files` (
  `id` varchar(255) NOT NULL,
  `file_path` varchar(1024) NOT NULL,
  `file_name` varchar(255) NOT NULL,
  `relative_path` varchar(1024) DEFAULT NULL,
  `mount` varchar(255) DEFAULT NULL,
  `file_size` bigint DEFAULT '0',
  `mtime` double DEFAULT '0',
  `status` varchar(50) DEFAULT 'PENDING',
  `vector_id` varchar(255) DEFAULT NULL,
  `jellyfin_id` varchar(255) DEFAULT NULL,
  `metadata_json` longtext,
  `updated_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_file_path` (`file_path`(255)),
  KEY `idx_mount` (`mount`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci

CREATE TABLE `llm_parsed_metadata` (
  `file_name` varchar(512) NOT NULL COMMENT 'normalized basename without extension, lowercased — reused across mounts',
  `full_path` varchar(1024) DEFAULT NULL,
  `song_title` varchar(512) DEFAULT NULL,
  `movie_or_album` varchar(512) DEFAULT NULL,
  `artists_json` text,
  `model_name` varchar(255) DEFAULT NULL,
  `source_endpoint` varchar(255) DEFAULT NULL,
  `parsed_at` datetime DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`file_name`),
  KEY `idx_full_path` (`full_path`(255))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci

CREATE TABLE `background_jobs` (
  `job_id` varchar(255) NOT NULL,
  `job_type` varchar(50) NOT NULL COMMENT 'llm_parse | duplicate_detect',
  `mount_name` varchar(255) DEFAULT NULL COMMENT 'NULL = all mounts',
  `status` varchar(50) DEFAULT 'PENDING' COMMENT 'PENDING|RUNNING|PAUSED|COMPLETED|FAILED|CANCELLED',
  `requested_status` varchar(50) DEFAULT NULL COMMENT 'set by API; runner polls this to pause/resume/cancel',
  `total_items` int DEFAULT '0',
  `processed_items` int DEFAULT '0',
  `failed_items` int DEFAULT '0',
  `checkpoint` varchar(1024) DEFAULT NULL COMMENT 'last processed file_name or mount name, for resume',
  `last_error` text,
  `created_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` timestamp NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`job_id`),
  KEY `idx_type_status` (`job_type`,`status`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci

CREATE TABLE `token_stats` (
  `phonetic_code` varchar(32) NOT NULL,
  `example_word` varchar(255) DEFAULT NULL,
  `tier` enum('title','movie','artist') DEFAULT 'title',
  `doc_frequency` int DEFAULT NULL,
  `updated_at` datetime DEFAULT NULL,
  PRIMARY KEY (`phonetic_code`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci