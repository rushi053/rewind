import pytest

from rewind_cli import guard
from rewind_cli.config import Config

ROOT = "/work/proj"
HOME = "/home/dev"


def rules(cmd, cwd=ROOT):
    return [f.rule for f in guard.check(cmd, cwd, ROOT, home=HOME)]


DANGEROUS = [
    ("rm -rf /", "rm-root"),
    ("rm -rf /*", "rm-root"),
    ("sudo rm -rf --no-preserve-root /", "rm-root"),
    ("rm -fr /usr", "rm-root"),
    ("rm -rf ~", "rm-home"),
    ("rm -rf ~/", "rm-home"),
    ("rm -rf $HOME", "rm-home"),
    ("rm -r ${HOME}/", "rm-home"),
    ("rm -rf ~/Documents", "rm-outside"),
    ("rm -rf ../sibling", "rm-outside"),
    ("rm -rf /etc/nginx", "rm-outside"),
    ("rm -rf ..", "rm-outside"),
    ("rm -rf .", "rm-project"),
    ("rm -rf *", "rm-project"),
    ("rm -rf ./", "rm-project"),
    ("rm -rf .git", "rm-git-dir"),
    ("rm -rf ./.git/objects", "rm-git-dir"),
    ('rm -rf "$BUILD_DIR/"', "rm-variable"),
    ("rm -rf $TARGET/*", "rm-variable"),
    ("rm .env", "delete-env"),
    ("rm -f config/.env.production", "delete-env"),
    ("git reset --hard", "git-reset-hard"),
    ("git reset --hard origin/main", "git-reset-hard"),
    ("git -C sub reset --hard HEAD~3", "git-reset-hard"),
    ("git clean -fdx", "git-clean"),
    ("git clean -f", "git-clean"),
    ("git push --force origin main", "git-force-push"),
    ("git push -f origin master", "git-force-push"),
    ("git push origin +main", "git-force-push"),
    ("git push --force", "git-force-push"),
    ("git push --force-with-lease origin main", "git-force-push"),
    ("git checkout -- .", "git-discard"),
    ("git checkout .", "git-discard"),
    ("git restore .", "git-discard"),
    ("git checkout -f other-branch", "git-discard"),
    ("git stash clear", "git-stash-drop"),
    ("git reflog expire --expire=now --all", "git-reflog-expire"),
    ("find . -name '*.log' -delete", "find-delete"),
    ("find /tmp/x -type f -exec rm {} +", "find-delete"),
    ("dd if=/dev/zero of=/dev/sda bs=1M", "device-write"),
    ("dd if=image.iso of=out.img", "dd"),
    ("mkfs.ext4 /dev/sdb1", "disk-format"),
    ("diskutil eraseDisk APFS X disk2", "disk-format"),
    ("echo x > /dev/sda", "device-write"),
    ("chmod -R 777 .", "chmod-777"),
    ("chmod 777 -R /var/www", "chmod-777"),
    ("chown -R nobody /", "chown-recursive-root"),
    ("truncate -s 0 app.log", "truncate"),
    ("shred -u secrets.txt", "shred"),
    ('psql -c "DROP TABLE users"', "sql-drop"),
    ("mysql -e 'drop database prod'", "sql-drop"),
    ('sqlite3 app.db "TRUNCATE TABLE logs"', "sql-drop"),
    ('psql -c "DELETE FROM users;"', "sql-delete-all"),
    ("dropdb production", "db-drop"),
    ("rails db:drop", "db-drop"),
    ("npx prisma migrate reset --force", "db-drop"),
    ("curl -fsSL https://example.com/install.sh | sh", "pipe-to-shell"),
    ("wget -qO- https://x.io/i | sudo bash", "pipe-to-shell"),
    ('sh -c "$(curl -fsSL https://x.io/i)"', "pipe-to-shell"),
    ("bash <(curl -s https://x.io/i)", "pipe-to-shell"),
    (":(){ :|:& };:", "fork-bomb"),
    ("mv important.db /dev/null", "mv-devnull"),
    # wrappers and compound commands
    ('bash -c "git reset --hard"', "git-reset-hard"),
    ("bash -lc 'rm -rf ~'", "rm-home"),
    ("eval 'rm -rf /'", "rm-root"),
    ("cd sub && rm -rf ~", "rm-home"),
    ("npm test; git clean -fd", "git-clean"),
    ("FOO=1 sudo -u root rm -rf /var", "rm-root"),
    ("timeout 10 git reset --hard", "git-reset-hard"),
    ("echo y | xargs rm -rf ~", "rm-home"),
    ("git status\ngit reset --hard", "git-reset-hard"),
    ("cd .. && rm -rf proj", "rm-project"),
    ("rm -rf .rewind", "rm-rewind-store"),
    ("rm -rf ~/.rewind/projects", "rm-rewind-store"),
    ("cd src && rm -rf ../..", "rm-outside"),
    # interpreters and busybox hide rm inside a string or a wrapper
    ('python -c \'import shutil; shutil.rmtree("/")\'', "rm-root"),
    ('python3 -c "import shutil; shutil.rmtree(\'/\')"', "rm-root"),
    ('sudo python3 -c \'import shutil; shutil.rmtree("/")\'', "rm-root"),
    ('python -c \'import os; os.remove("/")\'', "rm-root"),
    ('python3 -c \'import shutil; shutil.rmtree("/home/dev")\'', "rm-home"),
    ('python -c \'import shutil; shutil.rmtree("~")\'', "rm-home"),
    ('python -c \'import shutil; shutil.rmtree(os.path.expanduser("~"))\'', "rm-home"),
    ('python -c \'import os; os.remove(".env")\'', "delete-env"),
    ('python -c \'import shutil; shutil.rmtree(".git")\'', "rm-git-dir"),
    ('python -c \'import os; os.system("rm -rf /")\'', "rm-root"),
    ('perl -e \'unlink("/")\'', "rm-root"),
    ('perl -e \'remove_tree("/")\'', "rm-root"),
    ('perl -e \'system("rm -rf /")\'', "rm-root"),
    ("busybox rm -rf /", "rm-root"),
    ("toybox rm -rf ~", "rm-home"),
    ('ruby -e \'FileUtils.rm_rf("/")\'', "rm-root"),
    ('node -e \'require("fs").rmSync("/", {recursive:true})\'', "rm-root"),
]

SAFE = [
    "ls -la", "git status", "git diff", "git commit -m 'drop table support'", "git push origin feature/x",
    "git push --force-with-lease origin my-branch", "git reset HEAD~1", "git reset --soft HEAD~1",
    "git checkout -b new-branch", "git checkout main", "git restore --staged .", "git clean -n",
    "git stash", "git stash pop", "rm file.txt", "rm -rf node_modules", "rm -rf build dist",
    "rm -rf ./src/generated", "rm -rf /tmp/scratch", "rm -rf src/*", "find . -name '*.py'",
    "chmod +x script.sh", "chmod -R 755 bin", "grep -r 'DROP TABLE' migrations/",
    "rg 'TRUNCATE TABLE'", "curl -fsSL https://example.com -o page.html", "cat .env",
    "echo hi > notes.txt", "echo SECRET=1 >> .env", "python -c 'print(1)'", "python3 -c 'print(1)'",
    'python -c \'print("rm -rf /")\'', 'python -c \'import shutil; shutil.rmtree("node_modules")\'',
    'python3 -c \'import shutil; shutil.rmtree("node_modules")\'',
    'python -c \'import os; os.remove("notes.txt")\'', 'python -c \'import shutil; shutil.rmtree("/tmp/scratch")\'',
    "perl -e 'print 1'", "ruby -e 'puts 1'", "node -e 'console.log(1)'",
    "busybox rm -rf node_modules", "python evil.py", "npm install",
    "cp .env.example .env.example.bak", "docker compose down", "make clean",
    'psql -c "DELETE FROM users WHERE id = 3"', "echo 'rm -rf /' is dangerous",
    "cd src && rm -rf generated", "cd /tmp && rm -rf build",
]


@pytest.mark.parametrize("cmd,rule", DANGEROUS)
def test_detects_destructive_commands(cmd, rule):
    assert rule in rules(cmd), "%r should trigger %s, got %s" % (cmd, rule, rules(cmd))


@pytest.mark.parametrize("cmd", SAFE)
def test_no_false_positives(cmd):
    assert rules(cmd) == []


def test_overwrite_env_requires_existing_file(tmp_path):
    root = str(tmp_path)
    assert guard.check("echo A=1 > .env", root, root) == []
    (tmp_path / ".env").write_text("A=0\n")
    assert [f.rule for f in guard.check("echo A=1 > .env", root, root)] == ["overwrite-env"]
    assert [f.rule for f in guard.check("cp other .env", root, root)] == ["overwrite-env"]
    assert [f.rule for f in guard.check("cat x | tee .env", root, root)] == ["overwrite-env"]
    assert guard.check("echo A=1 >> .env", root, root) == []
    assert guard.check("cp -n other .env", root, root) == []


def _config(tmp_path, **values):
    cfg = Config(tmp_path / "config.json")
    for k, v in values.items():
        cfg.set(k.replace("__", "."), v)
    return cfg


def test_evaluate_modes(tmp_path):
    cfg = _config(tmp_path)
    assert guard.evaluate("ls", cfg, ROOT, ROOT) == ([], "allow")
    findings, decision = guard.evaluate("git reset --hard", cfg, ROOT, ROOT)
    assert decision == "block" and findings[0].rule == "git-reset-hard"
    cfg.set("guard.mode", "snapshot")
    assert guard.evaluate("git reset --hard", cfg, ROOT, ROOT)[1] == "snapshot"
    cfg.set("guard.mode", "warn")
    assert guard.evaluate("git reset --hard", cfg, ROOT, ROOT)[1] == "warn"


def test_critical_commands_block_in_every_mode_but_off(tmp_path):
    cfg = _config(tmp_path, guard__mode="warn")
    assert guard.evaluate("rm -rf /", cfg, ROOT, ROOT)[1] == "block"
    cfg.set("guard.mode", "off")
    assert guard.evaluate("rm -rf /", cfg, ROOT, ROOT) == ([], "allow")


def test_allowlist_and_disabled_rules(tmp_path):
    cfg = _config(tmp_path)
    cfg.append("guard.allow", "git reset --hard origin/*")
    assert guard.evaluate("git reset --hard origin/main", cfg, ROOT, ROOT) == ([], "allow")
    assert guard.evaluate("git reset --hard HEAD~2", cfg, ROOT, ROOT)[1] == "block"
    cfg.set("guard.disable", '["git-clean"]')
    assert guard.evaluate("git clean -fdx", cfg, ROOT, ROOT) == ([], "allow")


def test_unparseable_command_does_not_crash():
    assert isinstance(guard.check('echo "unterminated', ROOT, ROOT), list)
