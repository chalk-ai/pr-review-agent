from review_agent import commentable_lines, normalize_path

DIFF = """diff --git a/app.py b/app.py
index 1111111..2222222 100644
--- a/app.py
+++ b/app.py
@@ -10,4 +10,5 @@ def handler():
     a = 1
-    b = 2
+    b = 3
+    c = 4
     return a
diff --git a/new.py b/new.py
new file mode 100644
--- /dev/null
+++ b/new.py
@@ -0,0 +1,2 @@
+x = 1
+y = 2
diff --git a/gone.py b/gone.py
deleted file mode 100644
--- a/gone.py
+++ /dev/null
@@ -1,1 +0,0 @@
-z = 1
"""


def test_added_and_context_lines_are_right_side() -> None:
    lines = commentable_lines(DIFF)
    assert lines["app.py"]["RIGHT"] == {10, 11, 12, 13}


def test_deleted_and_context_lines_are_left_side() -> None:
    lines = commentable_lines(DIFF)
    assert lines["app.py"]["LEFT"] == {10, 11, 12}


def test_new_file_has_only_right_lines() -> None:
    lines = commentable_lines(DIFF)
    assert lines["new.py"] == {"RIGHT": {1, 2}, "LEFT": set()}


def test_deleted_file_is_not_commentable() -> None:
    assert "gone.py" not in commentable_lines(DIFF)


def test_line_outside_hunk_is_rejected() -> None:
    lines = commentable_lines(DIFF)
    assert 20 not in lines["app.py"]["RIGHT"]


def test_normalize_path_strips_mount_and_repo_prefixes() -> None:
    known = commentable_lines(DIFF)
    assert normalize_path("/review/repo/app.py", known) == "app.py"
    assert normalize_path("repo/app.py", known) == "app.py"
    assert normalize_path("./app.py", known) == "app.py"


def test_normalize_path_strips_git_prefix_only_when_it_resolves() -> None:
    known = commentable_lines(DIFF)
    assert normalize_path("b/app.py", known) == "app.py"
    assert normalize_path("a/missing.py", known) == "a/missing.py"
