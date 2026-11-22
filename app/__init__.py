"""Flask 复核网页：路由与页面逻辑。"""
from __future__ import annotations

import json
import os

from flask import (
    Flask,
    flash,
    g,
    redirect,
    render_template,
    request,
    url_for,
)

from . import db, demo, importer, stats
from .demo import DEMO_DRAWS_A, DEMO_DRAWS_B, DEMO_ROSTER_A, DEMO_ROSTER_B

DB_PATH = os.environ.get("APP_DB_PATH", "/data/review.db")


def create_app(db_path: str | None = None) -> Flask:
    app = Flask(__name__)
    app.config["SECRET_KEY"] = os.environ.get("APP_SECRET_KEY", "dev-secret")
    app.config["DB_PATH"] = db_path or DB_PATH
    app.jinja_env.filters["fromjson"] = json.loads
    os.makedirs(os.path.dirname(app.config["DB_PATH"]), exist_ok=True)

    def get_conn():
        if "conn" not in g:
            g.conn = db.connect(app.config["DB_PATH"])
            db.init_db(g.conn)
        return g.conn

    @app.teardown_appcontext
    def close_conn(exc):
        conn = g.pop("conn", None)
        if conn is not None:
            conn.close()

    def build_frame(conn) -> stats.Frame:
        meta = db.get_meta(conn)
        membership = {
            r["catalog_id"]: (bool(r["in_a"]), bool(r["in_b"]))
            for r in db.list_items(conn)
        }
        return stats.Frame(
            n_a=meta["n_a"],
            n_b=meta["n_b"],
            N_a=sum(1 for v in membership.values() if v[0]),
            N_b=sum(1 for v in membership.values() if v[1]),
            membership=membership,
        )

    @app.route("/")
    def index():
        conn = get_conn()
        locked = db.is_locked(conn)
        meta = db.get_meta(conn)
        pending = db.pending_count(conn) if locked else 0
        conflict = db.has_conflict(conn) if locked else False
        return render_template(
            "index.html",
            locked=locked,
            meta=meta,
            pending=pending,
            conflict=conflict,
            item_count=len(db.list_items(conn)) if locked else 0,
            snapshots=db.list_snapshots(conn),
        )

    @app.route("/import", methods=["GET", "POST"])
    def import_page():
        conn = get_conn()
        if db.is_locked(conn):
            flash("首次登记已完成，两份名录与中签号已锁定，不能再次导入。", "error")
            return redirect(url_for("index"))

        if request.method == "POST":
            if request.form.get("action") == "demo":
                return render_template(
                    "import.html",
                    form=dict(
                        roster_a=DEMO_ROSTER_A,
                        roster_b=DEMO_ROSTER_B,
                        draws_a=DEMO_DRAWS_A,
                        draws_b=DEMO_DRAWS_B,
                    ),
                )
            try:
                roster_a = importer.parse_roster(
                    request.form.get("roster_a", ""), "甲组"
                )
                roster_b = importer.parse_roster(
                    request.form.get("roster_b", ""), "乙组"
                )
                draws_a = importer.parse_draws(
                    request.form.get("draws_a", ""), "甲组"
                )
                draws_b = importer.parse_draws(
                    request.form.get("draws_b", ""), "乙组"
                )
                items, da, db_ = importer.validate_and_build(
                    roster_a, roster_b, draws_a, draws_b
                )
                db.import_dataset(conn, items, da, db_)
                flash(
                    f"导入成功并已锁定：并集 {len(items)} 件，"
                    f"甲组中签 {len(da)} 件，乙组中签 {len(db_)} 件。"
                )
                return redirect(url_for("index"))
            except importer.ImportError_ as exc:
                flash(f"导入失败：{exc}", "error")
            except Exception as exc:  # noqa: BLE001 - 页面兜底
                flash(f"导入失败：{exc}", "error")
            return render_template("import.html", form=request.form)

        return render_template("import.html", form={})

    @app.route("/register/<grp>", methods=["GET"])
    def register(grp: str):
        if grp not in ("A", "B"):
            flash("未知分组", "error")
            return redirect(url_for("index"))
        conn = get_conn()
        if not db.is_locked(conn):
            flash("请先导入并锁定名录与中签号。", "error")
            return redirect(url_for("import_page"))
        rows = db.get_draw(conn, grp)
        filled = sum(1 for r in rows if r["conclusion"] is not None)
        return render_template(
            "register.html", grp=grp, rows=rows, filled=filled
        )

    @app.route("/register/<grp>/bulk", methods=["POST"])
    def register_bulk(grp: str):
        """整表批量保存；未选择结论的藏品保持原状。"""
        if grp not in ("A", "B"):
            flash("未知分组", "error")
            return redirect(url_for("index"))
        conn = get_conn()
        if not db.is_locked(conn):
            flash("请先导入并锁定名录与中签号。", "error")
            return redirect(url_for("import_page"))
        allowed = {r["catalog_id"] for r in db.get_draw(conn, grp)}
        saved = 0
        for catalog_id in allowed:
            conclusion = request.form.get(f"conc-{catalog_id}")
            if conclusion not in ("error", "ok"):
                continue
            note = request.form.get(f"note-{catalog_id}", "").strip()
            db.upsert_registration(conn, grp, catalog_id, conclusion, note)
            saved += 1
        flash(f"{db.group_name(grp)}已保存 {saved} 条结论。")
        return redirect(url_for("register", grp=grp))

    @app.route("/register/<grp>/demo", methods=["POST"])
    def register_demo(grp: str):
        """一键填入演示结论（含一个冲突点，便于演示待核与更正）。"""
        if grp not in ("A", "B"):
            flash("未知分组", "error")
            return redirect(url_for("index"))
        conn = get_conn()
        if not db.is_locked(conn):
            flash("请先导入并锁定名录与中签号。", "error")
            return redirect(url_for("import_page"))
        demo_map = {"A": demo.DEMO_REG_A, "B": demo.DEMO_REG_B}[grp]
        for catalog_id, (conclusion, note) in demo_map.items():
            db.upsert_registration(conn, grp, catalog_id, conclusion, note)
        flash(f"已为{db.group_name(grp)}填入演示结论。")
        return redirect(url_for("register", grp=grp))

    @app.route("/review")
    def review():
        conn = get_conn()
        if not db.is_locked(conn):
            flash("请先导入并锁定名录与中签号。", "error")
            return redirect(url_for("import_page"))
        rows = db.review_rows(conn)
        return render_template("review.html", rows=rows)

    @app.route("/results")
    def results():
        conn = get_conn()
        if not db.is_locked(conn):
            flash("请先导入并锁定名录与中签号。", "error")
            return redirect(url_for("import_page"))
        frame = build_frame(conn)
        pending = db.pending_count(conn)
        conflict = db.has_conflict(conn)
        result = None
        if pending == 0 and not conflict:
            y = db.current_conclusions(conn)
            result = stats.estimate(frame, y)
        return render_template(
            "results.html",
            frame=frame,
            pending=pending,
            conflict=conflict,
            result=result,
        )

    @app.route("/snapshots", methods=["POST"])
    def create_snapshot():
        conn = get_conn()
        if not db.is_locked(conn):
            flash("请先导入并锁定名录与中签号。", "error")
            return redirect(url_for("import_page"))
        pending = db.pending_count(conn)
        conflict = db.has_conflict(conn)
        if pending or conflict:
            flash("仍有待核藏品（未补齐或结论冲突），不能出数。", "error")
            return redirect(url_for("review"))
        frame = build_frame(conn)
        y = db.current_conclusions(conn)
        result = stats.estimate(frame, y)
        payload = {
            "frame": {
                "n_a": frame.n_a,
                "n_b": frame.n_b,
                "N_a": frame.N_a,
                "N_b": frame.N_b,
                "union_size": len(frame.membership),
            },
            "y": y,
            "result": result,
        }
        snapshot_id = db.save_snapshot(conn, payload)
        flash(f"已保存出数时点快照（第 {snapshot_id} 次出数）。")
        return redirect(url_for("snapshot_detail", snapshot_id=snapshot_id))

    @app.route("/snapshots/<int:snapshot_id>")
    def snapshot_detail(snapshot_id: int):
        conn = get_conn()
        row = conn.execute(
            "SELECT * FROM snapshots WHERE id = ?", (snapshot_id,)
        ).fetchone()
        if row is None:
            flash("快照不存在", "error")
            return redirect(url_for("index"))
        payload = json.loads(row["payload"])
        return render_template(
            "snapshot.html", snapshot=row, payload=payload
        )

    @app.route("/audit")
    def audit():
        conn = get_conn()
        return render_template("audit.html", rows=db.get_audit(conn))

    @app.route("/reset", methods=["POST"])
    def reset():
        conn = get_conn()
        db.reset_all(conn)
        flash("已清空全部数据，可重新导入演示。")
        return redirect(url_for("index"))

    return app
