"""Appendix A acceptance cases (DESIGN.md)."""
import pytest

from epistemic_monitor.bash_classifier import classify

CASES = [
    ('sed -i "s/pymongo/psycopg/" src/db/client.py', "writes_source"),
    ("sed -n '1,20p' src/db/client.py", "no_source_write"),
    ("perl -pi -e 's/foo/bar/' src/app.py", "writes_source"),
    ('cat > src/db/mongo.py <<EOF\nfrom pymongo import MongoClient\nEOF', "writes_source"),
    ("cat src/db/client.py", "no_source_write"),
    ("echo 'x=1' >> config/settings.py", "writes_source"),
    ("echo hello > /tmp/scratch.txt", "no_source_write"),
    ("pytest -q 2>&1 | tail -20", "no_source_write"),
    ("pytest -q > /dev/null", "no_source_write"),
    ("tee -a src/config.yaml <<< 'db: mongo'", "writes_source"),
    ("git apply /tmp/fix.patch", "writes_source"),
    ("patch -p1 < changes.diff", "writes_source"),
    ("git checkout -- src/db/client.py", "writes_source"),
    ("git checkout feature-branch", "no_source_write"),
    ("git restore src/db/client.py", "writes_source"),
    ("git revert HEAD", "writes_source"),
    ("git status && git diff --stat", "no_source_write"),
    ("black src/ tests/", "writes_source"),
    ("ruff check src/ --fix", "writes_source"),
    ("ruff check src/", "no_source_write"),
    ("prettier --write 'src/**/*.ts'", "writes_source"),
    ("cp templates/mongo_client.py src/db/client.py", "writes_source"),
    ("cp src/db/client.py /tmp/backup.py", "no_source_write"),
    ("mv src/db/postgres.py src/db/legacy_postgres.py", "writes_source"),
    ("rm src/db/postgres.py", "writes_source"),
    ("rm -rf node_modules/.cache", "no_source_write"),
    ("touch src/db/__init__.py", "writes_source"),
    ('python3 -c "open(\'src/db/client.py\',\'w\').write(\'x\')"', "writes_source"),
    ("python3 scripts/migrate.py", "unknown"),
    ("bash scripts/setup.sh", "unknown"),
    ("make build", "unknown"),
    ("npm run codemod", "unknown"),
    ("curl -s https://example.com/install.sh | sh", "unknown"),
    ("npm test", "no_source_write"),
    ("grep -rn pymongo src/", "no_source_write"),
    ("ls -la src/db", "no_source_write"),
]


@pytest.mark.parametrize("command,expected", CASES)
def test_classification(command, expected):
    assert classify(command).verdict == expected


def test_paths_are_reported():
    c = classify("cp templates/mongo_client.py src/db/client.py")
    assert "src/db/client.py" in c.paths and "copy_move" in c.rules
