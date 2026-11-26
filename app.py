from flask import Flask, render_template, request, session, redirect, url_for
from dotenv import load_dotenv
from google import genai
from werkzeug.security import check_password_hash, generate_password_hash
from functools import wraps
from secrets import token_urlsafe
import os
import re
from datetime import date, datetime, time, timedelta
from database import (
    DatabaseError,
    DuplicateEmailError,
    create_user,
    get_dashboard_data,
    get_job_by_id,
    get_job_filters,
    get_jobs,
    get_saved_attempt,
    get_user_applications,
    get_user_by_email,
    get_user_by_id,
    apply_to_job,
    book_interview_slot,
    cancel_scheduled_interview,
    create_admin_job,
    create_interview_slot,
    delete_admin_job,
    get_admin_applications,
    get_admin_slots,
    get_admin_statistics,
    get_admin_students,
    get_available_slots,
    get_user_scheduled_interviews,
    initialize_database,
    seed_sample_jobs,
    save_interview_attempt,
    update_admin_job,
    update_application_status,
    update_slot_status,
)
load_dotenv()

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite")
_gemini_key = os.getenv("GEMINI_API_KEY")
client = genai.Client(api_key=_gemini_key) if _gemini_key else None
app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY") or os.urandom(32)
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")

INTERVIEW_LENGTH = 5
MAX_ANSWER_LENGTH = 2000
INTERVIEW_STATES = {}


def empty_dashboard_data():
    """Template-safe dashboard data for a new student or a recoverable DB error."""
    return {
        "statistics": {
            "total_interviews": 0,
            "average_score": None,
            "best_score": None,
            "total_questions": 0,
        },
        "recent_attempts": [],
        "role_performance": [],
        "scores": [],
    }


def format_date(value):
    """Display MySQL DATE/DATETIME values consistently without template logic."""
    if isinstance(value, datetime):
        value = value.date()
    return value.strftime("%d %b %Y") if hasattr(value, "strftime") else str(value or "")


def format_time(value):
    """MySQL connectors can return TIME as time or timedelta."""
    if isinstance(value, timedelta):
        seconds = int(value.total_seconds()) % 86400
        value = time(seconds // 3600, (seconds % 3600) // 60, seconds % 60)
    if isinstance(value, datetime):
        value = value.time()
    if isinstance(value, time):
        return value.strftime("%I:%M %p").lstrip("0")
    try:
        return datetime.strptime(str(value), "%H:%M:%S").strftime("%I:%M %p").lstrip("0")
    except ValueError:
        return str(value or "")


def format_datetime(value):
    if isinstance(value, datetime):
        return value.strftime("%d %b %Y, %I:%M %p").replace(" 0", " ")
    return format_date(value)


app.jinja_env.filters["display_date"] = format_date
app.jinja_env.filters["display_time"] = format_time
app.jinja_env.filters["display_datetime"] = format_datetime


def get_active_interview():
    interview_id = session.get("interview_id")
    return INTERVIEW_STATES.get(interview_id) if interview_id else None


def save_active_interview(interview):
    interview_id = session.get("interview_id")
    if not interview_id:
        interview_id = token_urlsafe(32)
        session["interview_id"] = interview_id
    INTERVIEW_STATES[interview_id] = interview


def clear_active_interview():
    interview_id = session.pop("interview_id", None)
    if interview_id:
        INTERVIEW_STATES.pop(interview_id, None)


def login_required(view):
    @wraps(view)
    def wrapped_view(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        return view(*args, **kwargs)

    return wrapped_view


def admin_required(view):
    @wraps(view)
    def wrapped_view(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login"))
        try:
            user = get_user_by_id(session["user_id"])
        except DatabaseError:
            user = None
        if not user or user["role"] != "admin":
            return render_template("access_denied.html"), 403
        return view(*args, **kwargs)
    return wrapped_view


def generate_interview_question(interview):
    if not client:
        raise ValueError("Gemini is not configured")
    previous_questions = "\n".join(
        f"- {question}" for question in interview["questions_asked"]
    ) or "None"

    prompt = f"""
    You are a professional interviewer.

    Conduct an interview for the following candidate:

    Job Role: {interview["job_role"]}
    Interview Type: {interview["interview_type"]}
    Difficulty Level: {interview["difficulty"]}

    Previously asked questions:
    {previous_questions}

    Generate ONE new interview question.
    Do not repeat any previously asked question.
    Do not give the answer.
    Keep the question professional and relevant to the selected role.
    """

    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=prompt,
    )
    question = response.text.strip() if response.text else ""

    if not question:
        raise ValueError("Gemini returned an empty response")

    return question


def render_interview(interview, error=None):
    return render_template(
        "interview.html",
        job_role=interview["job_role"],
        interview_type=interview["interview_type"],
        difficulty=interview["difficulty"],
        question=interview["current_question"],
        question_number=interview["question_number"],
        interview_length=INTERVIEW_LENGTH,
        error=error,
    )


def build_reviews(interview):
    return [
        {
            "number": number,
            "question": question,
            "answer": interview["candidate_answers"][number - 1],
            "score": interview["scores"][number - 1],
            "feedback": interview["feedback"][number - 1],
        }
        for number, question in enumerate(interview["questions_asked"], start=1)
    ]


def parse_overall_summary(summary):
    sections = {
        "performance_summary": "",
        "strengths": "",
        "weaknesses": "",
        "improvements": "",
    }
    headings = {
        "overall performance summary:": "performance_summary",
        "overall strengths:": "strengths",
        "overall weaknesses:": "weaknesses",
        "improvement suggestions:": "improvements",
    }
    current_section = None

    for line in summary.splitlines():
        section = headings.get(line.strip().lower())
        if section:
            current_section = section
        elif current_section:
            sections[current_section] += f"{line}\n"

    if not any(sections.values()):
        sections["performance_summary"] = summary

    return {key: value.strip() for key, value in sections.items()}


def generate_overall_summary(interview, average_score):
    if not client:
        raise ValueError("Gemini is not configured")
    review_details = "\n\n".join(
        f"Question {review['number']}: {review['question']}\n"
        f"Candidate Answer: {review['answer']}\n"
        f"Score: {review['score']}/10\n"
        f"Feedback: {review['feedback']}"
        for review in build_reviews(interview)
    )

    prompt = f"""
    You are a professional interview coach. Create a concise final assessment of a
    completed five-question interview.

    Job Role: {interview["job_role"]}
    Interview Type: {interview["interview_type"]}
    Difficulty Level: {interview["difficulty"]}
    Calculated Average Score: {average_score}/10

    Interview Review:
    {review_details}

    Base the assessment only on the supplied interview data. Respond in exactly
    this plain-text format:

    Overall Performance Summary:
    ...
    Overall Strengths:
    - ...
    Overall Weaknesses:
    - ...
    Improvement Suggestions:
    - ...
    """

    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=prompt,
    )
    summary = response.text.strip() if response.text else ""

    if not summary:
        raise ValueError("Gemini returned an empty response")

    return parse_overall_summary(summary)


def format_overall_summary(summary):
    if not summary:
        return None
    return "\n\n".join(
        (
            f"Overall Performance Summary:\n{summary['performance_summary']}",
            f"Overall Strengths:\n{summary['strengths']}",
            f"Overall Weaknesses:\n{summary['weaknesses']}",
            f"Improvement Suggestions:\n{summary['improvements']}",
        )
    )


@app.route("/")
def home():
    return render_template("index.html")


@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "GET":
        return render_template("signup.html", error=None)

    name = request.form.get("name", "").strip()
    email = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")
    confirm_password = request.form.get("confirm_password", "")

    if not all((name, email, password, confirm_password)):
        return render_template("signup.html", error="Please complete every field.")
    if "@" not in email or len(email) > 255:
        return render_template("signup.html", error="Please enter a valid email address.")
    if len(password) < 8:
        return render_template("signup.html", error="Password must be at least 8 characters.")
    if password != confirm_password:
        return render_template("signup.html", error="Passwords do not match.")

    try:
        create_user(name, email, generate_password_hash(password))
    except DuplicateEmailError:
        return render_template(
            "signup.html", error="An account with this email already exists."
        )
    except DatabaseError:
        return render_template(
            "signup.html", error="Account creation is unavailable right now."
        )

    return redirect(url_for("login", registered="1"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        return render_template(
            "login.html",
            error=None,
            message="Account created. Please log in." if request.args.get("registered") else None,
        )

    email = request.form.get("email", "").strip().lower()
    password = request.form.get("password", "")
    if not email or not password:
        return render_template("login.html", error="Enter your email and password.", message=None)

    try:
        user = get_user_by_email(email)
    except DatabaseError:
        return render_template(
            "login.html", error="Login is unavailable right now.", message=None
        )

    if not user or not check_password_hash(user["password_hash"], password):
        return render_template(
            "login.html", error="Invalid email or password.", message=None
        )

    session.clear()
    session["user_id"] = user["id"]
    session["user_name"] = user["name"]
    session["user_role"] = user["role"]
    if user["role"] == "admin":
    return redirect(url_for("admin_dashboard"))

return redirect(url_for("trainer"))


@app.route("/logout", methods=["POST"])
def logout():
    clear_active_interview()
    session.clear()
    return redirect(url_for("home"))


@app.route("/trainer")
@login_required
def trainer():
    return render_template("trainer.html")


@app.route("/jobs")
@login_required
def jobs():
    keyword = request.args.get("keyword", "").strip()
    location = request.args.get("location", "").strip()
    job_type = request.args.get("job_type", "").strip()
    try:
        initialize_database()
        seed_sample_jobs()
        listings = get_jobs(keyword, location, job_type)
        locations, job_types = get_job_filters()
        jobs_error = None
    except DatabaseError:
        listings, locations, job_types = [], [], []
        jobs_error = "Job listings are unavailable right now. Please try again later."
    return render_template(
        "jobs.html",
        listings=listings,
        locations=locations,
        job_types=job_types,
        filters={"keyword": keyword, "location": location, "job_type": job_type},
        jobs_error=jobs_error,
    )


@app.route("/jobs/<int:job_id>")
@login_required
def job_details(job_id):
    try:
        job = get_job_by_id(job_id)
    except DatabaseError:
        job = None
    if not job:
        return render_template("job_details.html", job=None, message=None, error="This job listing is unavailable.")
    return render_template(
        "job_details.html",
        job=job,
        message=request.args.get("message"),
        error=None,
    )


@app.route("/jobs/<int:job_id>/apply", methods=["POST"])
@login_required
def apply_job(job_id):
    try:
        outcome = apply_to_job(session["user_id"], job_id)
    except DatabaseError:
        return redirect(
            url_for(
                "job_details",
                job_id=job_id,
                message="The application could not be submitted right now. Please try again.",
            )
        )
    messages = {
        "applied": "Application submitted successfully.",
        "duplicate": "You have already applied to this job.",
        "not_found": "This job listing is unavailable.",
    }
    return redirect(url_for("job_details", job_id=job_id, message=messages[outcome]))


@app.route("/applications")
@login_required
def my_applications():
    try:
        applications = get_user_applications(session["user_id"])
        applications_error = None
    except DatabaseError:
        applications, applications_error = [], "Your applications are unavailable right now. Please try again later."
    return render_template(
        "applications.html", applications=applications, applications_error=applications_error
    )


@app.route("/scheduled-interviews")
@login_required
def scheduled_interviews():
    try:
        slots = get_available_slots()
        scheduled = get_user_scheduled_interviews(session["user_id"])
        error = None
    except DatabaseError:
        slots, scheduled, error = [], [], "Interview scheduling is unavailable right now."
    return render_template("scheduled_interviews.html", slots=slots, scheduled=scheduled, error=error, message=request.args.get("message"))


@app.route("/scheduled-interviews/<int:slot_id>/book", methods=["POST"])
@login_required
def book_slot(slot_id):
    try:
        outcome = book_interview_slot(session["user_id"], slot_id)
        message = "Interview scheduled successfully." if outcome == "scheduled" else "This interview slot is no longer available."
    except DatabaseError:
        message = "The interview could not be scheduled right now."
    return redirect(url_for("scheduled_interviews", message=message))


@app.route("/scheduled-interviews/<int:slot_id>/cancel", methods=["POST"])
@login_required
def cancel_slot(slot_id):
    try:
        cancelled = cancel_scheduled_interview(session["user_id"], slot_id)
        message = "Interview booking cancelled." if cancelled else "This interview cannot be cancelled."
    except DatabaseError:
        message = "The interview could not be cancelled right now."
    return redirect(url_for("scheduled_interviews", message=message))


@app.route("/admin")
@admin_required
def admin_dashboard():
    try:
        statistics = get_admin_statistics()
        error = None
    except DatabaseError:
        statistics, error = None, "Admin data is unavailable right now."
    return render_template("admin_dashboard.html", statistics=statistics, error=error)


@app.route("/admin/students")
@admin_required
def admin_students():
    try: students, error = get_admin_students(), None
    except DatabaseError: students, error = [], "Student records are unavailable right now."
    return render_template("admin_students.html", students=students, error=error)


def _job_form_data():
    return {key: request.form.get(key, "").strip() for key in ("title", "company", "location", "job_type", "salary", "description", "responsibilities", "requirements", "skills")}


@app.route("/admin/jobs", methods=["GET", "POST"])
@admin_required
def admin_jobs():
    message = None
    if request.method == "POST":
        data = _job_form_data()
        if not data["title"] or not data["company"] or not data["description"]:
            message = "Title, company, and description are required."
        else:
            try: create_admin_job(data); return redirect(url_for("admin_jobs", message="Job created successfully."))
            except DatabaseError: message = "The job could not be created right now."
    try: listings, error = get_jobs(), None
    except DatabaseError: listings, error = [], "Job records are unavailable right now."
    return render_template("admin_jobs.html", listings=listings, error=error or message, message=request.args.get("message"), edit_job=None)


def _admin_jobs_page(message=None, error=None, edit_job=None):
    try:
        listings = get_jobs()
    except DatabaseError:
        listings = []
        error = error or "Job records are unavailable right now."
    return render_template("admin_jobs.html", listings=listings, error=error, message=message, edit_job=edit_job)


@app.route("/admin/jobs/<int:job_id>/edit", methods=["GET", "POST"])
@admin_required
def admin_edit_job(job_id):
    try: job = get_job_by_id(job_id)
    except DatabaseError: job = None
    if not job: return redirect(url_for("admin_jobs", message="Job not found."))
    if request.method == "POST":
        data = _job_form_data()
        if not data["title"] or not data["company"] or not data["description"]:
            return _admin_jobs_page(error="Title, company, and description are required.", edit_job=job)
        try: update_admin_job(job_id, data); return redirect(url_for("admin_jobs", message="Job updated successfully."))
        except DatabaseError: return _admin_jobs_page(error="The job could not be updated.", edit_job=job)
    return _admin_jobs_page(edit_job=job)


@app.route("/admin/jobs/<int:job_id>/delete", methods=["POST"])
@admin_required
def admin_delete_job(job_id):
    try:
        outcome = delete_admin_job(job_id)
        message = {"deleted":"Job deleted successfully.","in_use":"This job has applications or interview slots and cannot be deleted.","not_found":"Job not found."}[outcome]
    except DatabaseError: message = "The job could not be deleted."
    return redirect(url_for("admin_jobs", message=message))


@app.route("/admin/applications")
@admin_required
def admin_applications():
    try: applications, error = get_admin_applications(), None
    except DatabaseError: applications, error = [], "Application records are unavailable right now."
    return render_template("admin_applications.html", applications=applications, error=error, message=request.args.get("message"))


@app.route("/admin/applications/<int:application_id>/status", methods=["POST"])
@admin_required
def admin_application_status(application_id):
    status=request.form.get("status", "")
    valid={"Applied","Shortlisted","Interview Scheduled","Selected","Rejected"}
    message="Invalid application status."
    if status in valid:
        try: message="Application status updated." if update_application_status(application_id,status) else "Application not found."
        except DatabaseError: message="The application status could not be updated."
    return redirect(url_for("admin_applications", message=message))


@app.route("/admin/interviews", methods=["GET", "POST"])
@admin_required
def admin_interviews():
    message=None
    if request.method == "POST":
        slot={key:request.form.get(key, "").strip() for key in ("job_id","interview_date","interview_time","end_time","interview_type","mode_location")}
        try:
            start_time = datetime.strptime(slot["interview_time"], "%H:%M").time()
            end_time = datetime.strptime(slot["end_time"], "%H:%M").time()
            if not all(slot.values()) or int(slot["job_id"]) < 1 or datetime.strptime(slot["interview_date"], "%Y-%m-%d").date() < date.today() or end_time <= start_time:
                raise ValueError
            slot["job_id"]=int(slot["job_id"])
            outcome=create_interview_slot(slot)
            messages = {"created": "Interview slot created.", "conflict": "This slot conflicts with an existing active slot.", "not_found": "The selected job no longer exists."}
            return redirect(url_for("admin_interviews", message=messages[outcome]))
        except (ValueError, TypeError): message="Enter a future date, valid job, and an end time after the start time."
        except DatabaseError: message="The interview slot could not be created."
    try: slots, listings, error = get_admin_slots(), get_jobs(), None
    except DatabaseError: slots, listings, error = [], [], "Interview slot data is unavailable right now."
    return render_template("admin_scheduled_interviews.html", slots=slots, listings=listings, error=error or message, message=request.args.get("message"))


@app.route("/admin/interviews/<int:slot_id>/status", methods=["POST"])
@admin_required
def admin_interview_status(slot_id):
    status=request.form.get("status", "")
    valid={"Available","Scheduled","Completed","Cancelled"}
    message="Invalid interview status."
    if status in valid:
        try: message="Interview status updated." if update_slot_status(slot_id,status) else "Interview slot not found."
        except DatabaseError: message="The interview status could not be updated."
    return redirect(url_for("admin_interviews", message=message))


@app.route("/mock-interview")
@login_required
def mock_interview():
    return render_template("mock_interview.html")


@app.route("/dashboard")
@login_required
def dashboard():
    try:
        profile = get_user_by_id(session["user_id"])
        dashboard_data = get_dashboard_data(session["user_id"])
    except DatabaseError:
        dashboard_data = empty_dashboard_data()
        return render_template(
            "dashboard.html",
            user={"name": session.get("user_name", "Student"), "email": ""},
            data=dashboard_data,
            profile=None,
            statistics=dashboard_data["statistics"],
            recent_attempts=dashboard_data["recent_attempts"],
            insights=None,
            dashboard_error="Your dashboard data is unavailable right now. Please try again later.",
        )

    if not profile:
        session.clear()
        return redirect(url_for("login"))

    statistics = dashboard_data["statistics"]
    total_interviews = statistics["total_interviews"]
    insights = None
    if total_interviews:
        role_performance = dashboard_data["role_performance"]
        scores = dashboard_data["scores"]
        strongest = role_performance[0]
        improvement = role_performance[-1]
        if len(scores) < 2:
            trend = "Complete another interview to see your performance trend."
        elif scores[-1] > scores[-2]:
            trend = "Your latest interview score improved from your previous attempt."
        elif scores[-1] < scores[-2]:
            trend = "Your latest interview score is below your previous attempt; use the feedback to target the next session."
        else:
            trend = "Your latest interview score is consistent with your previous attempt."
        insights = {
            "strongest_area": strongest["job_role"],
            "improvement_area": improvement["job_role"],
            "recommended_focus": (
                f"Practice {improvement['job_role'].replace('_', ' ')} questions and apply the suggestions from your latest feedback."
            ),
            "trend": trend,
            "progress_percent": min(round(float(statistics["average_score"]) * 10), 100),
        }

    return render_template(
        "dashboard.html",
        user=profile,
        data=dashboard_data,
        profile=profile,
        statistics=statistics,
        recent_attempts=dashboard_data["recent_attempts"],
        insights=insights,
        dashboard_error=None,
    )


@app.route("/results/<int:attempt_id>")
@login_required
def saved_results(attempt_id):
    try:
        attempt, reviews = get_saved_attempt(session["user_id"], attempt_id)
    except DatabaseError:
        attempt, reviews = None, []

    if not attempt:
        return render_template(
            "results.html",
            error="This saved interview result is unavailable.",
            interview={},
            reviews=[],
            total_questions=0,
            average_score=None,
            summary=None,
            summary_error=None,
            database_error=None,
        )

    # results.html expects the same shape produced by build_reviews().
    reviews = [
        {
            "number": row["question_number"],
            "question": row["question"],
            "answer": row["candidate_answer"],
            "score": row["score"],
            "feedback": row["feedback"],
        }
        for row in reviews
    ]

    summary = parse_overall_summary(attempt["overall_summary"]) if attempt["overall_summary"] else None
    return render_template(
        "results.html",
        error=None,
        interview=attempt,
        reviews=reviews,
        total_questions=attempt["total_questions"],
        average_score=float(attempt["average_score"]),
        summary=summary,
        summary_error=("No overall AI summary was saved for this interview." if not summary else None),
        database_error=None,
    )


@app.route("/start-interview", methods=["POST"])
@login_required
def start_interview():
    job_role = request.form.get("job_role", "").strip()
    interview_type = request.form.get("interview_type", "").strip()
    difficulty = request.form.get("difficulty", "").strip()
    valid_types = {"technical", "hr", "behavioral", "mixed"}
    valid_difficulties = {"beginner", "intermediate", "advanced"}
    if not job_role or len(job_role) > 100 or interview_type not in valid_types or difficulty not in valid_difficulties:
        return render_template("mock_interview.html", error="Choose a job role, interview type, and difficulty to begin.")

    clear_active_interview()
    session.pop("interview", None)
    session.pop("database_attempt_id", None)
    interview = {
        "job_role": job_role,
        "interview_type": interview_type,
        "difficulty": difficulty,
        "current_question": "",
        "question_number": 1,
        "questions_asked": [],
        "candidate_answers": [],
        "scores": [],
        "feedback": [],
        "user_id": session["user_id"],
    }

    try:
        interview["current_question"] = generate_interview_question(interview)
        interview["questions_asked"].append(interview["current_question"])
    except Exception:
        interview["current_question"] = "The interview question could not be generated right now."
        save_active_interview(interview)
        return render_interview(
            interview,
            error="Please start a new interview and try again.",
        )

    save_active_interview(interview)
    return render_interview(interview)


@app.route("/submit-answer", methods=["POST"])
@login_required
def submit_answer():
    request.form.get("job_role", "")
    request.form.get("interview_type", "")
    request.form.get("difficulty", "")
    request.form.get("current_question", "")
    candidate_answer = request.form.get("candidate_answer", "").strip()
    interview = get_active_interview()

    if not interview or interview.get("user_id") != session["user_id"]:
        return render_template(
            "feedback.html",
            error="Your interview session has expired. Please start a new interview.",
            feedback=None,
            can_continue=False,
            is_last_question=False,
            completed=False,
        )

    if not candidate_answer:
        return render_interview(interview, error="Please enter an answer before submitting.")
    if len(candidate_answer) > MAX_ANSWER_LENGTH:
        return render_interview(
            interview,
            error=f"Please keep your answer under {MAX_ANSWER_LENGTH} characters so your interview session is preserved.",
        )
    if len(interview.get("candidate_answers", [])) >= interview.get("question_number", 0):
        return render_interview(interview, error="This answer has already been submitted. Continue to the next question.")

    prompt = f"""
    You are a professional interview coach. Evaluate the candidate's answer.

    Job Role: {interview["job_role"]}
    Interview Type: {interview["interview_type"]}
    Difficulty Level: {interview["difficulty"]}
    Interview Question: {interview["current_question"]}
    Candidate Answer: {candidate_answer}

    Give constructive, specific feedback. Respond in exactly this plain-text format:

    Score: X/10
    Strengths:
    - ...
    Weaknesses:
    - ...
    Improvement Suggestions:
    - ...
    Overall Feedback: ...
    """

    try:
        if not client:
            raise ValueError("Gemini is not configured")
        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
        )
        feedback = response.text

        if not feedback:
            raise ValueError("Gemini returned an empty response")
    except Exception:
        return render_interview(
            interview,
            error="Feedback could not be generated right now. Please submit your answer again.",
        )

    score_match = re.search(
        r"Score:\s*(10(?:\.0+)?|[0-9](?:\.\d+)?)\s*/\s*10",
        feedback,
        re.IGNORECASE,
    )
    # Keep the five-question flow recoverable if the model misses the requested
    # score line. A visible zero is safer than a non-numeric database value.
    score = float(score_match.group(1)) if score_match else 0.0
    if not score_match:
        feedback = "Score: 0/10\n\n" + feedback + "\n\nNote: The AI response did not provide a usable score, so this answer was recorded as 0."
    interview["candidate_answers"].append(candidate_answer)
    interview["scores"].append(score)
    interview["feedback"].append(feedback)
    save_active_interview(interview)

    is_last_question = interview["question_number"] == INTERVIEW_LENGTH

    return render_template(
        "feedback.html",
        feedback=feedback,
        error=None,
        can_continue=not is_last_question,
        is_last_question=is_last_question,
        completed=False,
    )


@app.route("/next-question", methods=["POST"])
@login_required
def next_question():
    interview = get_active_interview()

    if not interview or interview.get("user_id") != session["user_id"]:
        return render_template(
            "feedback.html",
            error="Your interview session has expired. Please start a new interview.",
            feedback=None,
            can_continue=False,
            is_last_question=False,
            completed=False,
        )

    if len(interview.get("candidate_answers", [])) != interview["question_number"]:
        return render_template(
            "feedback.html",
            error="Submit an answer before continuing to the next question.",
            feedback=interview["feedback"][-1] if interview["feedback"] else None,
            can_continue=False,
            is_last_question=interview["question_number"] >= INTERVIEW_LENGTH,
            completed=False,
        )
    if interview["question_number"] >= INTERVIEW_LENGTH:
        return render_template("feedback.html", error="You have reached the final question. Please finish the interview.", feedback=interview["feedback"][-1], can_continue=False, is_last_question=True, completed=False)

    try:
        question = generate_interview_question(interview)
    except Exception:
        return render_template(
            "feedback.html",
            error="The next question could not be generated right now. Please try again.",
            feedback=None,
            can_continue=True,
            is_last_question=False,
            completed=False,
        )

    interview["question_number"] += 1
    interview["current_question"] = question
    interview["questions_asked"].append(question)
    save_active_interview(interview)
    return render_interview(interview)


@app.route("/finish-interview", methods=["POST"])
@login_required
def finish_interview():
    interview = get_active_interview()

    if not interview or interview.get("user_id") != session["user_id"]:
        return render_template(
            "feedback.html",
            error="Your interview session has expired. Please start a new interview.",
            feedback=None,
            can_continue=False,
            is_last_question=False,
            completed=False,
        )

    required_fields = (
        "questions_asked",
        "candidate_answers",
        "scores",
        "feedback",
    )
    if (
        any(len(interview.get(field, [])) != INTERVIEW_LENGTH for field in required_fields)
        or len(interview.get("scores", [])) != INTERVIEW_LENGTH
        or any(score is None for score in interview["scores"])
    ):
        return render_template(
            "results.html",
            error="Complete interview results are not available for this session.",
            interview=interview,
            reviews=[],
            total_questions=0,
            average_score=None,
            summary=None,
            summary_error=None,
        )

    reviews = build_reviews(interview)
    average_score = sum(interview["scores"]) / INTERVIEW_LENGTH

    try:
        summary = generate_overall_summary(interview, average_score)
        summary_error = None
    except Exception:
        summary = None
        summary_error = (
            "The overall AI summary could not be generated right now. "
            "Your calculated scores and question-by-question feedback are still shown below."
        )

    database_error = None
    if not session.get("database_attempt_id"):
        try:
            attempt_id = save_interview_attempt(
                interview,
                average_score,
                format_overall_summary(summary),
                session["user_id"],
            )
            session["database_attempt_id"] = attempt_id
        except DatabaseError:
            database_error = (
                "Your interview results could not be saved to the database right now."
            )

    return render_template(
        "results.html",
        error=None,
        interview=interview,
        reviews=reviews,
        total_questions=INTERVIEW_LENGTH,
        average_score=round(average_score, 2),
        summary=summary,
        summary_error=summary_error,
        database_error=database_error,
    )


@app.route("/start-new-interview", methods=["POST"])
@login_required
def start_new_interview():
    clear_active_interview()
    session.pop("interview", None)
    session.pop("database_attempt_id", None)
    return redirect(url_for("mock_interview"))


if __name__ == "__main__":
    # Keep debug disabled so normal users never receive diagnostic tracebacks.
    app.run(debug=False)
