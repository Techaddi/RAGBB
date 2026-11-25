import pytest
from app.schema_store import SchemaStore
from app.sql_guard import SqlRejected, validate_ddl, validate_select

S = SchemaStore("data/schema.json", "data/table_descriptions.json")
K = S.columns_map()
BLOCK = {"password_hash", "otp_hash"}


def ok(sql, **kw):
    return validate_select(sql, K, BLOCK, 200, **kw)


def test_valid_join_gets_row_cap():
    v = ok("SELECT u.name, COUNT(*) AS c FROM dbo.User_Mst u JOIN dbo.News_Post n ON n.user_id=u.user_id GROUP BY u.name ORDER BY c DESC")
    assert v.sql.startswith("SELECT TOP 200") and set(v.tables) == {"dbo.user_mst", "dbo.news_post"}


def test_existing_top_kept():
    assert "TOP 5" in ok("SELECT TOP 5 headline FROM News_Post").sql


def test_cte_and_union_ok():
    ok("WITH x AS (SELECT user_id, COUNT(*) cnt FROM News_Post GROUP BY user_id) SELECT u.name, x.cnt FROM User_Mst u JOIN x ON x.user_id=u.user_id")
    ok("SELECT name FROM User_Mst UNION SELECT name FROM Admin_UserMst")


@pytest.mark.parametrize("sql", [
    "DROP TABLE dbo.User_Mst",
    "DELETE FROM User_Mst",
    "SELECT * FROM User_Mst; DROP TABLE x",
    "SELECT * INTO #t FROM User_Mst",
    "EXEC xp_cmdshell 'dir'",
    "SELECT name FROM sys.tables",
    "SELECT name FROM OtherDb.dbo.User_Mst",
    "SELECT password_hash FROM Admin_UserMst",
    "SELECT * FROM Admin_UserMst",
    "SELECT name FROM Customers",
    "SELECT u.nam FROM User_Mst u",
    "SELECT headline FROM News_Post WHERE 1=1; WAITFOR DELAY '0:0:5'",
])
def test_rejected(sql):
    with pytest.raises(SqlRejected):
        ok(sql)


def test_proposed_table_allowed_only_when_declared():
    sql = "SELECT r.rating FROM dbo.Reviews r"
    with pytest.raises(SqlRejected):
        ok(sql)
    assert ok(sql, proposed={"dbo.reviews": {"rating"}}).tables == ["dbo.reviews"]


def test_ddl_validation():
    cols, ddl = validate_ddl(["CREATE TABLE dbo.Reviews (id INT PRIMARY KEY, rating INT)"], K, 5)
    assert "dbo.reviews" in cols and "CREATE TABLE" in ddl[0]
    with pytest.raises(SqlRejected):
        validate_ddl(["CREATE TABLE dbo.User_Mst (id INT)"], K, 5)       # already exists
    with pytest.raises(SqlRejected):
        validate_ddl(["DROP TABLE dbo.User_Mst"], K, 5)
    with pytest.raises(SqlRejected):
        validate_ddl(["CREATE TABLE dbo.A (id INT)"] * 6, K, 5)
