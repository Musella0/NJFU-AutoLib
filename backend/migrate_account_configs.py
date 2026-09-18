"""One-time migration to one globally unique configuration per student ID."""

import os
from collections import defaultdict
from datetime import datetime, timezone

from bson.json_util import dumps
from pymongo import MongoClient

from utils import config
from utils.account_config import default_account_config, merge_account_documents
from utils.crypto import encrypt


BACKUP_DIR = os.environ.get("ADMIN_DATA_DIR", "/app/data/backups")


def _write_encrypted_backup(documents) -> str:
    os.makedirs(BACKUP_DIR, mode=0o700, exist_ok=True)
    try:
        os.chmod(BACKUP_DIR, 0o700)
    except OSError:
        pass
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = os.path.join(BACKUP_DIR, f"user_config_info-{stamp}.json.enc")
    payload = encrypt(dumps(documents))
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(payload)
    return path


def main() -> None:
    client = MongoClient(config.get_mongo_uri())
    db = client.AutoLib
    collection = db.user_config_info
    documents = list(collection.find())
    backup_path = _write_encrypted_backup(documents)

    groups = defaultdict(list)
    for document in documents:
        pid = document.get("pid")
        if isinstance(pid, str) and pid:
            groups[pid].append(document)

    merged_groups = 0
    removed_duplicates = 0
    reowned_records = 0
    for pid, group in groups.items():
        canonical = max(
            group,
            key=lambda doc: getattr(
                doc.get("_id"),
                "generation_time",
                datetime.min.replace(tzinfo=timezone.utc),
            ),
        )
        merged = merge_account_documents(group, web_uid=pid, pid=pid)
        replacement = {"_id": canonical["_id"], **merged}
        collection.replace_one({"_id": canonical["_id"]}, replacement)
        if any(document.get("web_uid") != pid for document in group):
            reowned_records += 1
        duplicate_ids = [
            document["_id"]
            for document in group
            if document["_id"] != canonical["_id"]
        ]
        if duplicate_ids:
            result = collection.delete_many({"_id": {"$in": duplicate_ids}})
            removed_duplicates += result.deleted_count
        if len(group) > 1:
            merged_groups += 1

    defaults = default_account_config()
    backfilled = 0
    for document in collection.find():
        missing = {
            key: value
            for key, value in defaults.items()
            if key not in document
        }
        update = {}
        if missing:
            update["$set"] = missing
        if "lib_password" in document:
            update["$unset"] = {"lib_password": ""}
        if update:
            collection.update_one({"_id": document["_id"]}, update)
            backfilled += 1

    db.web_users.create_index("uid", unique=True, name="uniq_web_uid")
    if "uniq_owner_pid" in collection.index_information():
        collection.drop_index("uniq_owner_pid")
    collection.create_index(
        [("pid", 1)],
        unique=True,
        name="uniq_pid",
        partialFilterExpression={
            "pid": {"$type": "string"},
        },
    )
    client.close()

    print(f"BACKUP={backup_path}")
    print(f"MERGED_GROUPS={merged_groups}")
    print(f"REMOVED_DUPLICATES={removed_duplicates}")
    print(f"REOWNED_RECORDS={reowned_records}")
    print(f"BACKFILLED_RECORDS={backfilled}")
    print("INDEXES=ready")


if __name__ == "__main__":
    main()
