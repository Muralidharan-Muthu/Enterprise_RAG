from app.services.storage_service import _table_image_path


def test_table_image_path_format():
    assert _table_image_path("doc1", 3) == "tables/doc1/3.png"
