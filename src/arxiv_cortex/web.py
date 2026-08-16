from __future__ import annotations

import re

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from arxiv_cortex.db import get_db
from arxiv_cortex.security import csrf_token, validate_csrf
from arxiv_cortex.services.arxiv_sync import ArxivRequestError
from arxiv_cortex.services.papers import PaperQuery, PaperService
from arxiv_cortex.services.recommendations import RecommendationService
from arxiv_cortex.services.remote_search import RemoteSearchService
from arxiv_cortex.services.search_tags import SearchTagService
from arxiv_cortex.services.settings import SettingsService

web = Blueprint("web", __name__)
SEARCH_TAG_FORM_SESSION_KEY = "search_tag_form"

COMMON_CATEGORIES = [
    ("cs.AI", "Artificial Intelligence"),
    ("cs.CL", "Computation and Language"),
    ("cs.CV", "Computer Vision"),
    ("cs.LG", "Machine Learning"),
    ("cs.NE", "Neural and Evolutionary Computing"),
    ("cs.RO", "Robotics"),
    ("stat.ML", "Machine Learning (Statistics)"),
    ("math.OC", "Optimization and Control"),
    ("quant-ph", "Quantum Physics"),
]

# Researcher-facing groupings over arXiv's category taxonomy. Topics are kept
# unique across areas so a checkbox always has one clear home in the UI.
RESEARCH_AREAS = [
    {
        "id": "ai-data",
        "short": "AI",
        "name": "AI & Data",
        "description": "Learning, language, vision, and information retrieval",
        "topics": [
            ("cs.AI", "Artificial Intelligence"),
            ("cs.CL", "Language & NLP"),
            ("cs.CV", "Computer Vision"),
            ("cs.LG", "Machine Learning"),
            ("cs.NE", "Neural & Evolutionary Computing"),
            ("cs.IR", "Information Retrieval"),
            ("stat.ML", "Statistical Machine Learning"),
        ],
    },
    {
        "id": "computer-science",
        "short": "CS",
        "name": "Computer Science",
        "description": "Software, systems, security, theory, and interaction",
        "topics": [
            ("cs.AR", "Hardware Architecture"),
            ("cs.CC", "Computational Complexity"),
            ("cs.CR", "Cryptography & Security"),
            ("cs.DB", "Databases"),
            ("cs.DC", "Distributed & Parallel Computing"),
            ("cs.DS", "Data Structures & Algorithms"),
            ("cs.HC", "Human-Computer Interaction"),
            ("cs.IT", "Information Theory"),
            ("cs.NI", "Networking & Internet Architecture"),
            ("cs.OS", "Operating Systems"),
            ("cs.PL", "Programming Languages"),
            ("cs.SE", "Software Engineering"),
            ("cs.SI", "Social & Information Networks"),
        ],
    },
    {
        "id": "robotics",
        "short": "RO",
        "name": "Robotics & Autonomy",
        "description": "Robots, agents, control, and autonomous systems",
        "topics": [
            ("cs.RO", "Robotics"),
            ("cs.MA", "Multiagent Systems"),
            ("eess.SY", "Systems & Control"),
            ("math.OC", "Optimization & Control"),
        ],
    },
    {
        "id": "physics",
        "short": "PH",
        "name": "Physics & Astronomy",
        "description": "From quantum systems and materials to the universe",
        "topics": [
            ("astro-ph.CO", "Cosmology"),
            ("astro-ph.EP", "Earth & Planetary Astrophysics"),
            ("astro-ph.GA", "Galactic Astrophysics"),
            ("astro-ph.HE", "High-Energy Astrophysics"),
            ("astro-ph.IM", "Astrophysics Instrumentation"),
            ("astro-ph.SR", "Solar & Stellar Astrophysics"),
            ("cond-mat.mtrl-sci", "Materials Science"),
            ("cond-mat.stat-mech", "Statistical Mechanics"),
            ("gr-qc", "Relativity & Cosmology"),
            ("hep-ex", "High-Energy Physics — Experiment"),
            ("hep-ph", "High-Energy Physics — Phenomenology"),
            ("hep-th", "High-Energy Physics — Theory"),
            ("nucl-ex", "Nuclear Experiment"),
            ("nucl-th", "Nuclear Theory"),
            ("physics.atom-ph", "Atomic Physics"),
            ("physics.optics", "Optics"),
            ("quant-ph", "Quantum Physics"),
        ],
    },
    {
        "id": "mathematics",
        "short": "MA",
        "name": "Mathematics",
        "description": "Pure and applied mathematics, probability, and logic",
        "topics": [
            ("math.AG", "Algebraic Geometry"),
            ("math.AP", "Analysis of PDEs"),
            ("math.CO", "Combinatorics"),
            ("math.DG", "Differential Geometry"),
            ("math.DS", "Dynamical Systems"),
            ("math.FA", "Functional Analysis"),
            ("math.LO", "Logic"),
            ("math.NA", "Numerical Analysis"),
            ("math.NT", "Number Theory"),
            ("math.PR", "Probability"),
            ("math.ST", "Statistics Theory"),
        ],
    },
    {
        "id": "engineering",
        "short": "EE",
        "name": "Engineering & Systems",
        "description": "Signals, sensing, instrumentation, and applied physics",
        "topics": [
            ("eess.AS", "Audio & Speech Processing"),
            ("eess.IV", "Image & Video Processing"),
            ("eess.SP", "Signal Processing"),
            ("physics.acc-ph", "Accelerator Physics"),
            ("physics.app-ph", "Applied Physics"),
            ("physics.comp-ph", "Computational Physics"),
            ("physics.flu-dyn", "Fluid Dynamics"),
            ("physics.ins-det", "Instrumentation & Detectors"),
        ],
    },
    {
        "id": "biology",
        "short": "BI",
        "name": "Biology & Life Sciences",
        "description": "Quantitative biology, biophysics, and medical physics",
        "topics": [
            ("q-bio.BM", "Biomolecules"),
            ("q-bio.CB", "Cell Behavior"),
            ("q-bio.GN", "Genomics"),
            ("q-bio.MN", "Molecular Networks"),
            ("q-bio.NC", "Neurons & Cognition"),
            ("q-bio.PE", "Populations & Evolution"),
            ("q-bio.QM", "Quantitative Methods"),
            ("q-bio.SC", "Subcellular Processes"),
            ("q-bio.TO", "Tissues & Organs"),
            ("physics.bio-ph", "Biological Physics"),
            ("physics.med-ph", "Medical Physics"),
        ],
    },
    {
        "id": "economics-finance",
        "short": "EC",
        "name": "Economics & Finance",
        "description": "Economics, markets, risk, and quantitative finance",
        "topics": [
            ("econ.EM", "Econometrics"),
            ("econ.GN", "General Economics"),
            ("econ.TH", "Theoretical Economics"),
            ("q-fin.CP", "Computational Finance"),
            ("q-fin.GN", "General Finance"),
            ("q-fin.MF", "Mathematical Finance"),
            ("q-fin.PM", "Portfolio Management"),
            ("q-fin.PR", "Pricing of Securities"),
            ("q-fin.RM", "Risk Management"),
            ("q-fin.ST", "Statistical Finance"),
            ("q-fin.TR", "Trading & Market Microstructure"),
        ],
    },
    {
        "id": "statistics",
        "short": "ST",
        "name": "Statistics",
        "description": "Applied statistics, methods, computation, and theory",
        "topics": [
            ("stat.AP", "Applications"),
            ("stat.CO", "Computation"),
            ("stat.ME", "Methodology"),
            ("stat.OT", "Other Statistics"),
            ("stat.TH", "Statistics Theory"),
        ],
    },
    {
        "id": "policy-society",
        "short": "PS",
        "name": "Policy, Law & Society",
        "description": "Technology law, public policy, ethics, and society",
        "topics": [
            ("cs.CY", "Technology Policy & Law"),
            ("physics.soc-ph", "Physics & Society"),
            ("math.HO", "Mathematics History, Ethics & Education"),
            ("physics.hist-ph", "History & Philosophy of Physics"),
            ("physics.ed-ph", "Physics Education"),
        ],
    },
]
RESEARCH_AREA_CATEGORIES = {
    category for area in RESEARCH_AREAS for category, _label in area["topics"]
}


def _research_areas(selected: set[str]) -> list[dict[str, object]]:
    return [
        {
            **area,
            "selected_count": sum(category in selected for category, _label in area["topics"]),
        }
        for area in RESEARCH_AREAS
    ]


@web.before_app_request
def protect_forms() -> None:
    validate_csrf()


@web.app_context_processor
def inject_globals() -> dict[str, object]:
    connection = get_db()
    subscriptions = SettingsService(connection).subscriptions(enabled_only=True)
    followed_tags = SearchTagService(connection).list(followed_only=True)
    return {
        "csrf_token": csrf_token,
        "nav_categories": [row["category"] for row in subscriptions],
        "nav_followed_tags": followed_tags,
    }


@web.route("/")
def discover():
    connection = get_db()
    settings = SettingsService(connection)
    if not settings.has_feed_sources():
        return redirect(url_for("web.onboarding"))

    mode = request.args.get("mode", "newest")
    search_tags = SearchTagService(connection)
    tags = search_tags.list()
    active_tag = search_tags.get(_int_arg("tag", 0))
    query_text = request.args.get("q", "").strip()
    if active_tag and not query_text:
        query_text = active_tag["query"]
    category = request.args.get("category", "").strip()
    days = _int_arg("days", 0, allowed={0, 7, 30, 90, 365})
    page_number = max(1, _int_arg("page", 1))
    per_page = int(current_app.config["PER_PAGE"])
    offset = (page_number - 1) * per_page
    hide_read = request.args.get("hide_read") == "1"
    local_only_values = request.args.getlist("local_only")
    local_only = "1" in local_only_values if local_only_values else True
    papers = PaperService(connection)
    recommendations = RecommendationService(
        connection, current_app.extensions["embedding_index"]
    )
    reason = ""
    cold_start = False
    remote_error = ""
    searching_all_arxiv = bool(query_text and not local_only)

    if mode == "recommended" and not query_text:
        recommendation_days = days or int(settings.get("recommendation_days", "30") or 30)
        result = recommendations.recommend(
            category=category,
            days=recommendation_days,
            offset=offset,
            limit=per_page,
        )
        items = result.items
        cold_start = result.cold_start
        reason = result.reason
        has_next = len(items) == per_page
        total = 0
    elif mode == "similar":
        source_id = request.args.get("paper", "")
        if not source_id:
            abort(400, description="A source paper is required for similarity search")
        items = recommendations.similar(
            source_id,
            category=category,
            days=days,
            offset=offset,
            limit=per_page,
        )
        source = papers.get(source_id)
        reason = f"Papers closest to {source['title']}" if source else "Similar papers"
        has_next = len(items) == per_page
        total = 0
    elif searching_all_arxiv:
        mode = "search"
        try:
            remote_page = RemoteSearchService(
                connection, current_app.extensions["arxiv_source"]
            ).search(
                query_text,
                category=category,
                days=days,
                offset=offset,
                limit=per_page,
            )
        except (ArxivRequestError, ValueError):
            remote_error = (
                "arXiv search is temporarily unavailable. "
                "Showing matches already stored on this device instead."
            )
            result_page = papers.list(
                PaperQuery(
                    query=query_text,
                    category=category,
                    days=days,
                    hide_read=hide_read,
                    active_categories_only=False,
                    offset=offset,
                    limit=per_page,
                )
            )
            items = result_page.items
            has_next = result_page.has_next
            total = result_page.total
        else:
            items = [
                item
                for item in remote_page.items
                if not (hide_read and item["state"]["read"])
            ]
            current_app.extensions["job_manager"].submit_indexing()
            has_next = remote_page.has_next
            total = remote_page.total
    else:
        mode = "search" if query_text else "newest"
        result_page = papers.list(
            PaperQuery(
                query=query_text,
                category=category,
                days=days,
                hide_read=hide_read,
                active_categories_only=not bool(query_text),
                offset=offset,
                limit=per_page,
            )
        )
        items = result_page.items
        has_next = result_page.has_next
        total = result_page.total

    return render_template(
        "discover.html",
        papers=items,
        mode=mode,
        query_text=query_text,
        category=category,
        days=days,
        hide_read=hide_read,
        local_only=local_only,
        searching_all_arxiv=searching_all_arxiv,
        remote_error=remote_error,
        page_number=page_number,
        has_next=has_next,
        total=total,
        reason=reason,
        cold_start=cold_start,
        search_tags=tags,
        active_tag=active_tag,
    )


@web.route("/onboarding", methods=["GET", "POST"])
def onboarding():
    connection = get_db()
    settings = SettingsService(connection)
    tag_service = SearchTagService(connection)
    selected = {row["category"] for row in settings.subscriptions(enabled_only=True)}
    selected_tag_ids = {tag["id"] for tag in tag_service.list(followed_only=True)}
    custom_categories = ""
    tag_name = ""
    tag_description = ""
    tag_keywords = ""
    backfill_days = 90
    if request.method == "POST":
        categories = request.form.getlist("categories")
        custom_categories = request.form.get("custom_categories", "")
        categories.extend(_split_categories(custom_categories))
        selected = set(categories)
        tag_name = request.form.get("tag_name", "")
        tag_description = request.form.get("tag_description", "")
        tag_keywords = request.form.get("tag_keywords", "")
        try:
            selected_tag_ids = set(_form_int_list("tag_ids"))
            backfill_days = int(request.form.get("backfill_days", "90"))
            normalized_categories = settings.validate_subscriptions(categories, backfill_days)
            tag_service.validate_ids(list(selected_tag_ids))
            create_tag = bool(tag_name or tag_description or tag_keywords)
            if create_tag:
                tag_values = tag_service.validate_input(
                    tag_name, tag_description, tag_keywords, backfill_days
                )
                if connection.execute(
                    "SELECT 1 FROM search_tags WHERE name = ?", (tag_values[0],)
                ).fetchone():
                    raise ValueError("A research tag with that name already exists")
            if not normalized_categories and not selected_tag_ids and not create_tag:
                raise ValueError("Follow at least one keyword search or arXiv field")
            if create_tag:
                created = tag_service.create(
                    tag_name,
                    tag_description,
                    tag_keywords,
                    followed=True,
                    backfill_days=backfill_days,
                )
                selected_tag_ids.add(int(created["id"]))
            settings.update_subscriptions(normalized_categories, backfill_days)
            tag_service.set_followed(list(selected_tag_ids), backfill_days=backfill_days)
        except (TypeError, ValueError) as error:
            flash(str(error), "error")
        else:
            run_id = current_app.extensions["job_manager"].submit_sync("onboarding")
            return redirect(url_for("web.settings", run=run_id))
    return render_template(
        "onboarding.html",
        research_areas=_research_areas(selected),
        selected=selected,
        search_tags=tag_service.list(),
        selected_tag_ids=selected_tag_ids,
        custom_categories=custom_categories,
        tag_name=tag_name,
        tag_description=tag_description,
        tag_keywords=tag_keywords,
        backfill_days=backfill_days,
    )


@web.route("/papers/<path:arxiv_id>")
def paper_detail(arxiv_id: str):
    papers = PaperService(get_db())
    paper = papers.get(arxiv_id)
    if not paper:
        abort(404)
    related = RecommendationService(
        get_db(), current_app.extensions["embedding_index"]
    ).similar(arxiv_id, limit=8)
    return render_template("paper.html", paper=paper, related=related)


@web.route("/library")
def library():
    page_number = max(1, _int_arg("page", 1))
    per_page = int(current_app.config["PER_PAGE"])
    read_status = request.args.get("read", "")
    if read_status not in {"", "read", "unread"}:
        read_status = ""
    days = _int_arg("days", 0, allowed={0, 30, 90, 365})
    connection = get_db()
    result = PaperService(connection).list(
        PaperQuery(
            query=request.args.get("q", "").strip(),
            category=request.args.get("category", "").strip(),
            days=days,
            state="saved",
            read_status=read_status or None,
            active_categories_only=False,
            offset=(page_number - 1) * per_page,
            limit=per_page,
        )
    )
    library_categories = [
        row["category"]
        for row in connection.execute(
            """
            SELECT DISTINCT pc.category
            FROM paper_categories pc
            JOIN paper_state ps ON ps.paper_id = pc.paper_id
            WHERE ps.saved_at IS NOT NULL
            ORDER BY pc.category
            """
        )
    ]
    return render_template(
        "library.html",
        papers=result.items,
        read_status=read_status,
        days=days,
        library_categories=library_categories,
        query_text=request.args.get("q", "").strip(),
        category=request.args.get("category", "").strip(),
        page_number=page_number,
        has_next=result.has_next,
        total=result.total,
    )


@web.route("/settings", methods=["GET", "POST"])
def settings():
    connection = get_db()
    service = SettingsService(connection)
    if request.method == "POST":
        categories = request.form.getlist("categories")
        categories.extend(_split_categories(request.form.get("custom_categories", "")))
        try:
            service.update_subscriptions(categories, 90)
            service.update_runtime_settings(
                request.form.get("sync_time", "06:00"),
                int(request.form.get("recommendation_days", "30")),
            )
        except (TypeError, ValueError) as error:
            flash(str(error), "error")
        else:
            scheduler = current_app.extensions["sync_scheduler"]
            if scheduler.scheduler.running:
                scheduler.reload()
            if service.has_feed_sources():
                run_id = current_app.extensions["job_manager"].submit_sync("settings")
                flash("Settings saved. New feed sources will be backfilled in the background.", "success")
                return redirect(url_for("web.settings", run=run_id))
            flash("Settings saved. Syncing is paused until you follow a keyword search or field.", "success")
            return redirect(url_for("web.settings"))

    subscriptions = service.subscriptions()
    enabled = {row["category"] for row in subscriptions if row["enabled"]}
    custom = sorted(enabled - RESEARCH_AREA_CATEGORIES)
    latest_run = connection.execute("SELECT * FROM sync_runs ORDER BY id DESC LIMIT 1").fetchone()
    model_id = service.get("embedding_model", "") or ""
    stats = {
        "papers": PaperService(connection).count(),
        "embeddings": PaperService(connection).embedding_count(model_id),
        "model": model_id,
    }
    return render_template(
        "settings.html",
        research_areas=_research_areas(enabled),
        enabled=enabled,
        custom_categories=", ".join(custom),
        subscriptions=subscriptions,
        sync_time=service.get("sync_time", "06:00"),
        recommendation_days=int(service.get("recommendation_days", "30") or 30),
        latest_run=dict(latest_run) if latest_run else None,
        active_run_id=request.args.get("run"),
        stats=stats,
        search_tags=SearchTagService(connection).list(),
        search_tag_form=session.pop(SEARCH_TAG_FORM_SESSION_KEY, None),
    )


@web.post("/settings/tags")
def create_search_tag():
    followed = request.form.get("followed") == "1"
    try:
        tag = SearchTagService(get_db()).create(
            request.form.get("name", ""),
            request.form.get("description", ""),
            request.form.get("keywords", ""),
            followed=followed,
        )
    except ValueError as error:
        session[SEARCH_TAG_FORM_SESSION_KEY] = {
            "kind": "create",
            "error": str(error),
            "name": request.form.get("name", ""),
            "description": request.form.get("description", ""),
            "keywords": request.form.get("keywords", ""),
            "followed": followed,
        }
    else:
        if tag["followed"]:
            run_id = current_app.extensions["job_manager"].submit_sync("tag-created")
            flash(f"Following “{tag['name']}”. Its matching papers will now be fetched.", "success")
            return redirect(f"{url_for('web.settings', run=run_id)}#research-tags")
        flash(f"Created saved search “{tag['name']}”.", "success")
    return redirect(f"{url_for('web.settings')}#research-tags")


@web.post("/settings/tags/<int:tag_id>")
def update_search_tag(tag_id: int):
    service = SearchTagService(get_db())
    existing = service.get(tag_id)
    if not existing:
        abort(404)
    followed = request.form.get("followed") == "1"
    try:
        tag = service.update(
            tag_id,
            request.form.get("name", ""),
            request.form.get("description", ""),
            request.form.get("keywords", ""),
            followed=followed,
        )
    except LookupError:
        abort(404)
    except ValueError as error:
        session[SEARCH_TAG_FORM_SESSION_KEY] = {
            "kind": "update",
            "tag_id": tag_id,
            "error": str(error),
            "name": request.form.get("name", ""),
            "description": request.form.get("description", ""),
            "keywords": request.form.get("keywords", ""),
            "followed": followed,
        }
    else:
        query_changed = tag["keywords"] != existing["keywords"]
        newly_followed = tag["followed"] and not existing["followed"]
        if tag["followed"] and (query_changed or newly_followed):
            run_id = current_app.extensions["job_manager"].submit_sync("tag-updated")
            flash(f"Updated and queued “{tag['name']}” for synchronization.", "success")
            return redirect(f"{url_for('web.settings', run=run_id)}#research-tags")
        action = "Following" if tag["followed"] else "Saved"
        flash(f"{action} research tag “{tag['name']}”.", "success")
    return redirect(f"{url_for('web.settings')}#research-tags")


@web.post("/settings/tags/<int:tag_id>/delete")
def delete_search_tag(tag_id: int):
    service = SearchTagService(get_db())
    tag = service.get(tag_id)
    if not tag:
        abort(404)
    service.delete(tag_id)
    flash(f"Deleted research tag “{tag['name']}”.", "success")
    return redirect(f"{url_for('web.settings')}#research-tags")


@web.post("/settings/sync")
def start_sync():
    if not SettingsService(get_db()).has_feed_sources():
        abort(400, description="Follow a keyword search or arXiv field before refreshing")
    run_id = current_app.extensions["job_manager"].submit_sync("manual")
    if request.headers.get("HX-Request"):
        row = get_db().execute("SELECT * FROM sync_runs WHERE id = ?", (run_id,)).fetchone()
        return render_template("_job_status.html", run=dict(row))
    return redirect(url_for("web.settings", run=run_id))


@web.get("/jobs/<int:run_id>")
def job_status(run_id: int):
    row = get_db().execute("SELECT * FROM sync_runs WHERE id = ?", (run_id,)).fetchone()
    if not row:
        abort(404)
    return render_template("_job_status.html", run=dict(row))


@web.post("/papers/<path:arxiv_id>/save")
def save_paper(arxiv_id: str):
    paper = PaperService(get_db()).set_saved(arxiv_id, request.form.get("active") == "1")
    return _state_response(paper)


@web.post("/papers/<path:arxiv_id>/read")
def read_paper(arxiv_id: str):
    paper = PaperService(get_db()).set_read(arxiv_id, request.form.get("active") == "1")
    return _state_response(paper)


@web.post("/papers/<path:arxiv_id>/dismiss")
def dismiss_paper(arxiv_id: str):
    paper = PaperService(get_db()).set_dismissed(
        arxiv_id, request.form.get("active") == "1"
    )
    return _state_response(paper)


@web.get("/papers/<path:arxiv_id>/open/<kind>")
def open_paper(arxiv_id: str, kind: str):
    if kind not in {"abstract", "pdf"}:
        abort(404)
    paper = PaperService(get_db()).mark_opened(arxiv_id)
    return redirect(paper["links"][kind])


@web.app_errorhandler(400)
def bad_request(error):
    if request.headers.get("HX-Request"):
        return render_template("_error.html", message=error.description), 400
    return render_template("error.html", title="Bad request", message=error.description), 400


@web.app_errorhandler(404)
def not_found(_error):
    return render_template("error.html", title="Not found", message="That paper or page was not found."), 404


@web.app_errorhandler(500)
def server_error(_error):
    return render_template(
        "error.html", title="Something went wrong", message="The operation could not be completed."
    ), 500


def _state_response(paper: dict):
    if request.headers.get("HX-Request"):
        return render_template("_paper_actions.html", paper=paper)
    return redirect(request.referrer or url_for("web.discover"))


def _split_categories(value: str) -> list[str]:
    return [item for item in re.split(r"[,\s]+", value.strip()) if item]


def _int_arg(name: str, default: int, allowed: set[int] | None = None) -> int:
    try:
        value = int(request.args.get(name, str(default)))
    except ValueError:
        return default
    return value if allowed is None or value in allowed else default


def _form_int_list(name: str) -> list[int]:
    values: list[int] = []
    for raw_value in request.form.getlist(name):
        try:
            value = int(raw_value)
        except ValueError as error:
            raise ValueError("A selected research tag is invalid") from error
        if value > 0:
            values.append(value)
    return values
