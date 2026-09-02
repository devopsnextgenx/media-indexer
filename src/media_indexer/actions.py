import os
import shutil
import logging
import subprocess
from fastapi import HTTPException
from media_indexer.config import settings
from media_indexer.database import db_instance, mysql_db_instance, redis_db_instance
from media_indexer.utils import generate_file_id, normalize_text

logger = logging.getLogger(__name__)

class MediaActions:
    @staticmethod
    def download_yt(url: str, output_mount: str = "mount1") -> dict:
        target_dir = os.path.join(settings.mounts.base_dir, output_mount)
        os.makedirs(target_dir, exist_ok=True)
        
        output_template = os.path.join(target_dir, "%(title)s.%(ext)s")
        cmd = [
            "yt-dlp",
            "--no-mtime",
            "-f", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
            "-o", output_template,
            url
        ]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, check=True)
            return {"status": "success", "message": "Video downloaded successfully", "log": res.stdout[-300:]}
        except subprocess.CalledProcessError as e:
            logger.error(f"yt-dlp failed: {e.stderr}")
            raise HTTPException(status_code=500, detail=f"Download failed: {e.stderr[-300:]}")

    @staticmethod
    def _sync_index_after_rename(old_path: str, new_path: str) -> int:
        """Repoints existing vectors, MySQL DB records, and Redis cached tree nodes at the new file location."""
        new_name = os.path.basename(new_path)
        base_updates = {
            "file_path": new_path,
            "file_name": new_name,
            "normalized_title": normalize_text(os.path.splitext(new_name)[0]),
        }

        points = db_instance.find_points_by_file_path(old_path)
        if not points:
            points = db_instance.find_points_by_field("file_name", os.path.basename(old_path))

        updated_ids = set()
        for point in points:
            updates = dict(base_updates)
            payload = point.payload or {}
            rel_path = payload.get("relative_path")
            if rel_path:
                new_rel = os.path.join(os.path.dirname(rel_path), new_name)
                updates["relative_path"] = new_rel
            db_instance.update_payload_for_points([point.id], updates)
            updated_ids.add(str(point.id))

        legacy_id = generate_file_id(old_path)
        if legacy_id not in updated_ids:
            db_instance.delete_media_item(legacy_id)

        # Update MySQL Database (media_files, llm_parsed_metadata, duplicate_group_candidates)
        mysql_updated = mysql_db_instance.update_file_path(old_path, new_path, new_name)

        # Update Redis Database (mount tree nodes)
        redis_db_instance.rename_file_node(old_path, new_path)

        if points:
            logger.info(f"Updated {len(points)} vector payload(s) and {mysql_updated} MySQL record(s) for renamed file: {new_path}")
        else:
            logger.warning(f"No vector payload found for renamed file: {old_path}")
        return len(points)

    @staticmethod
    def bulk_rename_remove_underscores(directory: str | None = None) -> dict:
        target_dir = directory or settings.mounts.base_dir
        if not os.path.exists(target_dir):
            raise HTTPException(status_code=400, detail="Target path does not exist")

        renamed_files = []
        for root, _, files in os.walk(target_dir):
            for file in files:
                if "_" in file:
                    old_path = os.path.join(root, file)
                    new_filename = file.replace("_", " ")
                    new_path = os.path.join(root, new_filename)

                    shutil.move(old_path, new_path)
                    MediaActions._sync_index_after_rename(old_path, new_path)

                    renamed_files.append({"old": old_path, "new": new_path})

        return {"status": "success", "count": len(renamed_files), "renamed": renamed_files}

    @staticmethod
    def rename_file(old_path: str, new_name: str) -> dict:
        if not os.path.exists(old_path):
            raise HTTPException(status_code=404, detail="Original file not found")

        if os.path.basename(new_name) != new_name:
            raise HTTPException(status_code=400, detail="New name cannot contain path separators")

        dirname = os.path.dirname(old_path)
        new_path = os.path.join(dirname, new_name)

        if os.path.exists(new_path):
            raise HTTPException(status_code=409, detail="A file with that name already exists")

        shutil.move(old_path, new_path)
        updated = MediaActions._sync_index_after_rename(old_path, new_path)

        return {"status": "success", "new_path": new_path, "index_updated": updated}

    @staticmethod
    def delete_file(file_path: str) -> dict:
        if os.path.exists(file_path):
            os.remove(file_path)

        # Synchronize Vector DB
        removed = db_instance.delete_by_file_path(file_path)
        if not removed:
            removed = db_instance.delete_by_file_name(os.path.basename(file_path))

        db_instance.delete_media_item(generate_file_id(file_path))

        # Synchronize MySQL DB
        mysql_removed = mysql_db_instance.delete_file_by_path(file_path)

        # Synchronize Redis DB
        redis_removed = redis_db_instance.remove_file_node(file_path)

        if removed or mysql_removed or redis_removed:
            logger.info(f"Removed {removed} vector point(s), {mysql_removed} MySQL record(s), and {redis_removed} Redis node(s) for deleted file: {file_path}")
        else:
            logger.warning(f"No vector point, MySQL record, or Redis node found for deleted file: {file_path}")

        return {
            "status": "success",
            "message": f"Deleted {file_path}",
            "index_removed": removed,
            "mysql_removed": mysql_removed,
            "redis_removed": redis_removed,
        }

    @staticmethod
    def clean_record_from_index(file_path: str) -> dict:
        """Removes records from Qdrant, MySQL DB, and Redis without deleting the disk file."""
        # Clean Vector DB
        removed = db_instance.delete_by_file_path(file_path)
        if not removed:
            removed = db_instance.delete_by_file_name(os.path.basename(file_path))
        
        db_instance.delete_media_item(generate_file_id(file_path))

        # Clean MySQL DB
        mysql_removed = mysql_db_instance.delete_file_by_path(file_path)

        # Clean Redis DB
        redis_removed = redis_db_instance.remove_file_node(file_path)

        logger.info(f"Cleaned index: {removed} vector point(s), {mysql_removed} MySQL record(s), and {redis_removed} Redis node(s) for path: {file_path}")

        return {
            "status": "success",
            "message": f"Cleaned index records for {file_path}",
            "vector_removed": removed,
            "mysql_removed": mysql_removed,
            "redis_removed": redis_removed,
        }