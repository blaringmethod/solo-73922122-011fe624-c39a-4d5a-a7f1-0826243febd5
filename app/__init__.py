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

from . import db, demo, importer, state
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

    def load_state() -> state.State:
        return state.build_state(get_conn())

    def require_locked():
        st = load_state()
        if not st.locked:
            flash("请先导入并锁定名录与中签号。", "error")
            return None
        return st

    def stale_snapshot_message() -> str:
        return (
            "拆分状态或登记结论已在本次取数后发生变化，"
            "请刷新出数页后重新取数再保存快照。"
        )

    def state_changed_after_view(st: state.State) -> bool:
        """页面上带的取数令牌（当时的修订号）是否已不是当前修订号。

        未带令牌时返回 None（无法判断来源，交由调用方处理）。
        """
        raw = request.form.get("revision")
        if raw is None:
            return None
        try:
            token_revision = int(raw)
        except ValueError:
            return None
        return token_revision != st.revision

    @app.route("/")
    def index():
        conn = get_conn()
        locked = db.is_locked(conn)
        meta = db.get_meta(conn)
        st = load_state()
        pending = state.pending_count(st) if locked else 0
        conflict = state.has_conflict(st) if locked else False
        return render_template(
            "index.html",
            locked=locked,
            meta=meta,
            pending=pending,
            conflict=conflict,
            item_count=len(st.units) if locked else 0,
            split_count=len(st.splits) if locked else 0,
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
                db.import_dataset(
                    conn, items, da, db_, roster_a=roster_a, roster_b=roster_b
                )
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
        st = require_locked()
        if st is None:
            return redirect(url_for("import_page"))
        rows = db.get_draw(get_conn(), grp)
        filled = sum(1 for r in rows if r["conclusion"] is not None)
        split_ids = set(st.splits)
        return render_template(
            "register.html",
            grp=grp,
            rows=rows,
            filled=filled,
            split_ids=split_ids,
            piece=state.PIECE_NAMES[grp],
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
        st = require_locked()
        if st is None:
            return redirect(url_for("import_page"))
        rows = state.review_rows(st)
        shared = []
        for cid in st.shared_ids:
            split = cid in st.splits
            shared.append(
                {
                    "catalog_id": cid,
                    "title": st.item_titles.get(cid, ""),
                    "title_a": st.roster_titles.get(("A", cid), ""),
                    "title_b": st.roster_titles.get(("B", cid), ""),
                    "drawn_a": cid in st.draws.get("A", set()),
                    "drawn_b": cid in st.draws.get("B", set()),
                    "split": split,
                }
            )
        return render_template(
            "review.html",
            rows=rows,
            shared=shared,
            split_ids=sorted(st.splits),
            pending=state.pending_count(st),
            conflict=state.has_conflict(st),
            revision=st.revision,
        )

    @app.route("/review/split", methods=["POST"])
    def split_registration():
        conn = get_conn()
        if not db.is_locked(conn):
            flash("请先导入并锁定名录与中签号。", "error")
            return redirect(url_for("import_page"))
        catalog_id = (request.form.get("catalog_id") or "").strip()
        operator = (request.form.get("operator") or "").strip()
        redirect_to = request.form.get("next") or url_for("review")
        try:
            db.split_item(conn, catalog_id, operator)
            flash(
                f"已把共有编号 {catalog_id} 拆为甲件、乙件；"
                "两组原名录、中签号与各自登记均保留，"
                "两件分别进入待核判定。"
            )
        except ValueError as exc:
            flash(f"拆分失败：{exc}", "error")
        if not redirect_to.startswith("/"):
            redirect_to = url_for("review")
        return redirect(redirect_to)

    @app.route("/review/merge", methods=["POST"])
    def merge_registration():
        conn = get_conn()
        if not db.is_locked(conn):
            flash("请先导入并锁定名录与中签号。", "error")
            return redirect(url_for("import_page"))
        catalog_id = (request.form.get("catalog_id") or "").strip()
        try:
            db.merge_item(conn, catalog_id)
        except ValueError as exc:
            flash(f"并回失败：{exc}", "error")
            return redirect(url_for("review"))
        st = state.build_state(conn)
        # 并回后两组的登记原样保留；若同号两组结论冲突，会经重算重新进入待核
        rows = state.review_rows(st)
        still_pending = any(
            r["catalog_id"] == catalog_id and "两组结论冲突" in r["reasons"]
            for r in rows
        )
        if still_pending:
            flash(
                f"已并回 {catalog_id}；同号两组结论仍然冲突，"
                "该编号重新列入待核，请核对后更正登记。",
                "error",
            )
        else:
            flash(f"已并回 {catalog_id}；同号按一件计入，两组登记保持一致。")
        return redirect(url_for("review"))

    @app.route("/results")
    def results():
        conn = get_conn()
        if not db.is_locked(conn):
            flash("请先导入并锁定名录与中签号。", "error")
            return redirect(url_for("import_page"))
        st = load_state()
        pending = state.pending_count(st)
        conflict = state.has_conflict(st)
        computed = state.compute(st) if state.can_estimate(st) else None
        return render_template(
            "results.html",
            frame=state.build_frame(st),
            pending=pending,
            conflict=conflict,
            result=computed["result"] if computed else None,
            unit_meta=computed["unit_meta"] if computed else None,
            revision=st.revision,
            splits=st.splits,
        )

    @app.route("/snapshots", methods=["POST"])
    def create_snapshot():
        conn = get_conn()
        if not db.is_locked(conn):
            flash("请先导入并锁定名录与中签号。", "error")
            return redirect(url_for("import_page"))
        st = load_state()

        # 先判断本次取数之后状态是否变过：任何拆分/并回或登记变更
        # （包括改回原值）都会推进修订号，必须优先提示刷新，
        # 而不是笼统地说“仍有待核”。
        changed = state_changed_after_view(st)
        if changed is None:
            # 没有令牌（直接调用接口）：只能要求显式确认本次取数的修订号
            changed = True
        if changed:
            flash(stale_snapshot_message(), "error")
            return redirect(url_for("results"))

        pending = state.pending_count(st)
        conflict = state.has_conflict(st)
        if pending or conflict:
            flash("仍有待核藏品（未补齐或结论冲突），不能出数。", "error")
            return redirect(url_for("review"))

        computed = state.compute(st)
        frame = computed["frame"]
        payload = {
            "revision": st.revision,
            "frame": {
                "n_a": frame.n_a,
                "n_b": frame.n_b,
                "N_a": frame.N_a,
                "N_b": frame.N_b,
                "union_size": len(frame.membership),
            },
            "y": computed["y"],
            "result": computed["result"],
            "unit_meta": computed["unit_meta"],
            "split_ids": sorted(st.splits),
        }
        try:
            snapshot_id = db.save_snapshot(conn, payload, st.revision)
        except db.SnapshotStale:
            flash(stale_snapshot_message(), "error")
            return redirect(url_for("results"))
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
        return render_template(
            "audit.html",
            rows=db.get_audit(conn),
            split_rows=db.get_split_audit(conn),
        )

    @app.route("/reset", methods=["POST"])
    def reset():
        conn = get_conn()
        db.reset_all(conn)
        flash("已清空全部数据，可重新导入演示。")
        return redirect(url_for("index"))

    return app
