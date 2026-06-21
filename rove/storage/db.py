import sqlite3
import json
import os
import logging
from pathlib import Path

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

DB_PATH = "output/db/site_graph.db"
PAGES_DIR = "output/pages/"

def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.executescript('''
        DROP TABLE IF EXISTS links;
        DROP TABLE IF EXISTS elements;
        DROP TABLE IF EXISTS pages;
        CREATE TABLE pages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT,
            title TEXT,
            depth INTEGER,
            crawled_at TEXT,
            fingerprint TEXT,
            parent_state TEXT,
            screenshot_path TEXT,
            priority_score INTEGER,
            UNIQUE(url, fingerprint)
        );
        CREATE TABLE elements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            page_id INTEGER REFERENCES pages(id),
            tag TEXT,
            elem_type TEXT,
            text TEXT,
            frame_path TEXT,
            shadow_path TEXT,
            locators_json TEXT
        );
        CREATE TABLE links (
            from_page_id INTEGER REFERENCES pages(id),
            to_page_id INTEGER REFERENCES pages(id),
            transition_type TEXT DEFAULT 'link',
            via_element TEXT,
            PRIMARY KEY (from_page_id, to_page_id, transition_type)
        );
    ''')
    conn.commit()
    return conn

def import_pages():
    conn = init_db()
    cursor = conn.cursor()

    page_files = list(Path(PAGES_DIR).glob("*.json"))
    logger.info(f"Found {len(page_files)} page files to import.")

    # First pass: insert pages, build key->db_id map
    key_to_id = {}
    for file_path in page_files:
        data = json.loads(file_path.read_text(encoding="utf-8"))
        fp = data.get("fingerprint")
        cursor.execute(
            """INSERT OR IGNORE INTO pages
               (url, title, depth, crawled_at, fingerprint, parent_state,
                screenshot_path, priority_score)
               VALUES (?,?,?,?,?,?,?,?)""",
            (data["url"], data.get("title"), data["depth"], data["timestamp"],
             fp, data.get("parent_state"),
             data.get("screenshot_path"), data.get("priority_score")),
        )
        cursor.execute(
            "SELECT id FROM pages WHERE url=? AND (fingerprint IS ? OR (fingerprint IS NULL AND ? IS NULL))",
            (data["url"], fp, fp),
        )
        row = cursor.fetchone()
        if row:
            db_id = row[0]
            json_key = data.get("page_id", data["url"])
            key_to_id[json_key] = db_id
            key_to_id.setdefault(data["url"], db_id)

    conn.commit()

    # Second pass: elements and links
    for file_path in page_files:
        data = json.loads(file_path.read_text(encoding="utf-8"))
        json_key = data.get("page_id", data["url"])
        page_id = key_to_id.get(json_key)
        if page_id is None:
            continue

        for elem in data.get("elements", []):
            cursor.execute(
                """INSERT INTO elements
                   (page_id, tag, elem_type, text, frame_path, shadow_path, locators_json)
                   VALUES (?,?,?,?,?,?,?)""",
                (page_id, elem["tag"], elem["type"], elem["text"],
                 elem.get("frame_path"), elem.get("shadow_path"),
                 json.dumps(elem["locators"])),
            )

        for link_url in data.get("links", []):
            to_id = key_to_id.get(link_url)
            if to_id:
                try:
                    cursor.execute(
                        """INSERT OR IGNORE INTO links
                           (from_page_id, to_page_id, transition_type)
                           VALUES (?,?, 'link')""",
                        (page_id, to_id),
                    )
                except sqlite3.Error as e:
                    logger.error(f"Error inserting link: {e}")

        # Click-transition edges from SPA state discovery
        parent = data.get("parent_state")
        if parent and parent in key_to_id:
            try:
                via = (data.get("transition") or {}).get("via_element")
                cursor.execute(
                    """INSERT OR IGNORE INTO links
                       (from_page_id, to_page_id, transition_type, via_element)
                       VALUES (?,?, 'click', ?)""",
                    (key_to_id[parent], page_id, via),
                )
            except sqlite3.Error as e:
                logger.error(f"Error inserting click edge: {e}")

    conn.commit()

    cursor.execute("SELECT COUNT(*) FROM pages")
    total_pages = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM elements")
    total_elems = cursor.fetchone()[0]
    cursor.execute("SELECT COUNT(*) FROM links")
    total_links = cursor.fetchone()[0]

    conn.close()
    return total_pages, total_elems, total_links

if __name__ == "__main__":
    try:
        p, e, l = import_pages()
        print(f"Import complete.")
        print(f"Total pages: {p}")
        print(f"Total elements: {e}")
        print(f"Total links: {l}")
    except Exception as ex:
        logger.error(f"Import failed: {ex}")
