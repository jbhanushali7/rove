from rove.session.base import SessionStore
from rove.session import FileSessionStore


def test_file_store_is_a_session_store():
    assert issubclass(FileSessionStore, SessionStore)


def test_roundtrip(tmp_path):
    store = FileSessionStore(path=str(tmp_path / "s.json"))
    state = {"cookies": [{"name": "a", "value": "b", "expires": -1}], "origins": []}
    store.save_state(state)
    assert store.load() == state
