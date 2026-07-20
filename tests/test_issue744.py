from tests.frontend_asset_contract import family_source


def test_only_latest_user_message_gets_edit_button():
    src = family_source("ui")
    assert "let lastUserRawIdx=-1;" in src
    assert "const isEditableUser=isUser&&rawIdx===lastUserRawIdx;" in src
    assert "const editBtn  = isEditableUser ?" in src

