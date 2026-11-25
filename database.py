import os
import re

import mysql.connector
from mysql.connector import Error


class DatabaseError(Exception):
    """Safe application-level database error."""


class DuplicateEmailError(DatabaseError):
    """Raised when a signup email already exists."""


def _settings():
    settings = {
        "host": os.getenv("MYSQL_HOST"),
        "port": os.getenv("MYSQL_PORT", "3306"),
        "user": os.getenv("MYSQL_USER"),
        "password": os.getenv("MYSQL_PASSWORD"),
        "database": os.getenv("MYSQL_DATABASE"),
    }
    if any(
        not value or (isinstance(value, str) and value.startswith("YOUR_MYSQL_"))
        for value in settings.values()
    ):
        raise DatabaseError("MySQL environment variables have not been configured.")

    try:
        settings["port"] = int(settings["port"])
    except ValueError as error:
        raise DatabaseError("MYSQL_PORT must be a valid number.") from error

    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", settings["database"]):
        raise DatabaseError("MYSQL_DATABASE contains invalid characters.")
    return settings


def get_connection(include_database=True):
    settings = _settings()
    connection_settings = {
        "host": settings["host"],
        "port": settings["port"],
        "user": settings["user"],
        "password": settings["password"],
    }
    if include_database:
        connection_settings["database"] = settings["database"]
    try:
        return mysql.connector.connect(**connection_settings)
    except Error as error:
        raise DatabaseError("Could not connect to the MySQL database.") from error


def initialize_database():
    settings = _settings()
    database_name = settings["database"]
    server_connection = get_connection(include_database=False)
    cursor = server_connection.cursor()
    try:
        cursor.execute(
            f"CREATE DATABASE IF NOT EXISTS `{database_name}` "
            "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
        )
    except Error as error:
        raise DatabaseError("Could not create the MySQL database.") from error
    finally:
        cursor.close()
        server_connection.close()

    connection = get_connection()
    cursor = connection.cursor()
    table_statements = [
        """CREATE TABLE IF NOT EXISTS users (
            id INT AUTO_INCREMENT PRIMARY KEY,
            name VARCHAR(100) NOT NULL,
            email VARCHAR(255) NOT NULL UNIQUE,
            password_hash VARCHAR(255) NOT NULL,
            role VARCHAR(50) NOT NULL DEFAULT 'candidate',
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        ) ENGINE=InnoDB""",
        """CREATE TABLE IF NOT EXISTS interview_attempts (
            id INT AUTO_INCREMENT PRIMARY KEY,
            user_id INT NOT NULL,
            job_role VARCHAR(100) NOT NULL,
            interview_type VARCHAR(100) NOT NULL,
            difficulty VARCHAR(50) NOT NULL,
            total_questions INT NOT NULL,
            average_score DECIMAL(4,2) NOT NULL,
            overall_summary TEXT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT fk_attempt_user
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
        ) ENGINE=InnoDB""",
        """CREATE TABLE IF NOT EXISTS interview_responses (
            id INT AUTO_INCREMENT PRIMARY KEY,
            attempt_id INT NOT NULL,
            question_number INT NOT NULL,
            question TEXT NOT NULL,
            candidate_answer TEXT NOT NULL,
            score DECIMAL(4,2) NULL,
            feedback TEXT NOT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT fk_response_attempt
                FOREIGN KEY (attempt_id) REFERENCES interview_attempts(id) ON DELETE CASCADE
        ) ENGINE=InnoDB""",
        """CREATE TABLE IF NOT EXISTS jobs (
            id INT AUTO_INCREMENT PRIMARY KEY,
            title VARCHAR(255) NOT NULL,
            company VARCHAR(255) NOT NULL,
            location VARCHAR(255) NULL,
            description TEXT NULL,
            skills TEXT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        ) ENGINE=InnoDB""",
        """CREATE TABLE IF NOT EXISTS job_applications (
            id INT AUTO_INCREMENT PRIMARY KEY,
            user_id INT NOT NULL,
            job_id INT NOT NULL,
            status VARCHAR(50) NOT NULL DEFAULT 'applied',
            applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT fk_application_user
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
            CONSTRAINT fk_application_job
                FOREIGN KEY (job_id) REFERENCES jobs(id) ON DELETE CASCADE
        ) ENGINE=InnoDB""",
        """CREATE TABLE IF NOT EXISTS scheduled_interviews (
            id INT AUTO_INCREMENT PRIMARY KEY,
            user_id INT NOT NULL,
            job_id INT NOT NULL,
            interview_date DATE NOT NULL,
            interview_time TIME NOT NULL,
            interview_type VARCHAR(100) NOT NULL,
            status VARCHAR(50) NOT NULL DEFAULT 'scheduled',
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            CONSTRAINT fk_scheduled_user
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
            CONSTRAINT fk_scheduled_job
                FOREIGN KEY (job_id) REFERENCES jobs(id) ON DELETE CASCADE
        ) ENGINE=InnoDB""",
    ]
    try:
        for statement in table_statements:
            cursor.execute(statement)
        connection.commit()
    except Error as error:
        connection.rollback()
        raise DatabaseError("Could not create the required MySQL tables.") from error
    finally:
        cursor.close()
        connection.close()

    _ensure_job_schema()
    _ensure_user_and_schedule_schema()


def _ensure_job_schema():
    connection = get_connection()
    cursor = connection.cursor()
    try:
        cursor.execute("SHOW COLUMNS FROM jobs")
        columns = {row[0] for row in cursor.fetchall()}
        additions = {
            "job_type": "VARCHAR(100) NULL AFTER location",
            "salary": "VARCHAR(100) NULL AFTER job_type",
            "responsibilities": "TEXT NULL AFTER description",
            "requirements": "TEXT NULL AFTER responsibilities",
        }
        for name, definition in additions.items():
            if name not in columns:
                cursor.execute(f"ALTER TABLE jobs ADD COLUMN {name} {definition}")

        cursor.execute("SHOW INDEX FROM job_applications WHERE Key_name = 'uq_user_job_application'")
        existing_index = cursor.fetchall()
        if not existing_index:
            cursor.execute(
                "ALTER TABLE job_applications ADD UNIQUE KEY uq_user_job_application (user_id, job_id)"
            )
        connection.commit()
    except Error as error:
        connection.rollback()
        raise DatabaseError("Could not update the job database schema.") from error
    finally:
        cursor.close()
        connection.close()


def _ensure_user_and_schedule_schema():
    connection = get_connection()
    cursor = connection.cursor()
    try:
        cursor.execute("SHOW COLUMNS FROM users")
        user_columns = {row[0] for row in cursor.fetchall()}
        if "role" not in user_columns:
            cursor.execute("ALTER TABLE users ADD COLUMN role VARCHAR(50) NOT NULL DEFAULT 'student'")
        else:
            cursor.execute("ALTER TABLE users MODIFY COLUMN role VARCHAR(50) NOT NULL DEFAULT 'student'")
        cursor.execute("UPDATE users SET role = 'student' WHERE role = 'candidate'")

        cursor.execute("SHOW COLUMNS FROM scheduled_interviews")
        columns = {row[0] for row in cursor.fetchall()}
        cursor.execute("ALTER TABLE scheduled_interviews MODIFY COLUMN user_id INT NULL")
        additions = {
            "end_time": "TIME NULL AFTER interview_time",
            "mode_location": "VARCHAR(255) NULL AFTER interview_type",
            "capacity": "INT NOT NULL DEFAULT 1 AFTER mode_location",
        }
        for name, definition in additions.items():
            if name not in columns:
                cursor.execute(f"ALTER TABLE scheduled_interviews ADD COLUMN {name} {definition}")
        cursor.execute("UPDATE scheduled_interviews SET end_time = ADDTIME(interview_time, '01:00:00') WHERE end_time IS NULL")
        cursor.execute("UPDATE scheduled_interviews SET status = 'Available' WHERE user_id IS NULL AND status = 'scheduled'")
        connection.commit()
    except Error as error:
        connection.rollback()
        raise DatabaseError("Could not update the user and interview scheduling schema.") from error
    finally:
        cursor.close()
        connection.close()


def seed_sample_jobs():
    connection = get_connection()
    cursor = connection.cursor()
    try:
        cursor.execute("SELECT COUNT(*) FROM jobs")
        if cursor.fetchone()[0]:
            return
        cursor.executemany(
            """INSERT INTO jobs
               (title, company, location, job_type, salary, description,
                responsibilities, requirements, skills)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            [
                (
                    "Junior Python Developer", "NovaByte Technologies", "Bengaluru",
                    "Full-time", "₹4.5–6.5 LPA",
                    "Build reliable backend features for AI-enabled web applications.",
                    "Develop Python services; write tests; collaborate with product and engineering teams.",
                    "Bachelor's degree or final-year student; Python fundamentals; basic SQL knowledge.",
                    "Python, Flask, MySQL, REST APIs, Git",
                ),
                (
                    "Data Analyst Intern", "InsightGrid Labs", "Hyderabad",
                    "Internship", "₹20,000/month",
                    "Turn business data into clear dashboards and useful recommendations.",
                    "Clean datasets; create reports; support data-quality checks and stakeholder analysis.",
                    "Final-year student; analytical mindset; familiarity with spreadsheets or SQL.",
                    "SQL, Excel, Python, Power BI, Statistics",
                ),
                (
                    "Software Test Engineer", "Orbit Systems", "Pune",
                    "Full-time", "₹4–6 LPA",
                    "Help deliver dependable software through thoughtful testing and defect analysis.",
                    "Design test cases; execute manual tests; document defects; assist automation efforts.",
                    "Understanding of SDLC and testing concepts; detail-oriented communication.",
                    "Manual Testing, Selenium, Python, Jira, SQL",
                ),
            ],
        )
        connection.commit()
    except Error as error:
        connection.rollback()
        raise DatabaseError("Could not prepare sample job listings.") from error
    finally:
        cursor.close()
        connection.close()


def get_job_filters():
    connection = get_connection()
    cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute("SELECT DISTINCT location FROM jobs WHERE location IS NOT NULL ORDER BY location")
        locations = [row["location"] for row in cursor.fetchall()]
        cursor.execute("SELECT DISTINCT job_type FROM jobs WHERE job_type IS NOT NULL ORDER BY job_type")
        job_types = [row["job_type"] for row in cursor.fetchall()]
        return locations, job_types
    except Error as error:
        raise DatabaseError("Could not retrieve job filters.") from error
    finally:
        cursor.close()
        connection.close()


def get_jobs(keyword="", location="", job_type=""):
    clauses, parameters = [], []
    if keyword:
        clauses.append("(title LIKE %s OR company LIKE %s OR skills LIKE %s OR description LIKE %s)")
        parameters.extend([f"%{keyword}%"] * 4)
    if location:
        clauses.append("location = %s")
        parameters.append(location)
    if job_type:
        clauses.append("job_type = %s")
        parameters.append(job_type)
    where_clause = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    connection = get_connection()
    cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute(
            """SELECT id, title, company, location, job_type, salary, skills,
                      description, created_at FROM jobs""" + where_clause + " ORDER BY created_at DESC, id DESC",
            tuple(parameters),
        )
        return cursor.fetchall()
    except Error as error:
        raise DatabaseError("Could not retrieve job listings.") from error
    finally:
        cursor.close()
        connection.close()


def get_job_by_id(job_id):
    connection = get_connection()
    cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute("SELECT * FROM jobs WHERE id = %s", (job_id,))
        return cursor.fetchone()
    except Error as error:
        raise DatabaseError("Could not retrieve the job listing.") from error
    finally:
        cursor.close()
        connection.close()


def apply_to_job(user_id, job_id):
    connection = get_connection()
    cursor = connection.cursor()
    try:
        cursor.execute("SELECT id FROM jobs WHERE id = %s", (job_id,))
        if not cursor.fetchone():
            return "not_found"
        cursor.execute(
            "INSERT INTO job_applications (user_id, job_id, status) VALUES (%s, %s, %s)",
            (user_id, job_id, "Applied"),
        )
        connection.commit()
        return "applied"
    except Error as error:
        connection.rollback()
        if getattr(error, "errno", None) == 1062:
            return "duplicate"
        raise DatabaseError("Could not submit the job application.") from error
    finally:
        cursor.close()
        connection.close()


def get_user_applications(user_id):
    connection = get_connection()
    cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute(
            """SELECT ja.id, ja.status, ja.applied_at, j.id AS job_id, j.title,
                      j.company, j.location, j.job_type
               FROM job_applications AS ja
               INNER JOIN jobs AS j ON j.id = ja.job_id
               WHERE ja.user_id = %s
               ORDER BY ja.applied_at DESC, ja.id DESC""",
            (user_id,),
        )
        return cursor.fetchall()
    except Error as error:
        raise DatabaseError("Could not retrieve job applications.") from error
    finally:
        cursor.close()
        connection.close()


def get_available_slots():
    connection = get_connection()
    cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute(
            """SELECT si.id, si.interview_date, si.interview_time, si.end_time,
                      si.interview_type, si.mode_location, si.status, j.title, j.company
               FROM scheduled_interviews si
               LEFT JOIN jobs j ON j.id = si.job_id
               WHERE si.user_id IS NULL AND si.status = 'Available'
               AND (si.interview_date > CURDATE() OR (si.interview_date = CURDATE() AND si.interview_time >= CURTIME()))
               ORDER BY si.interview_date, si.interview_time"""
        )
        return cursor.fetchall()
    except Error as error:
        raise DatabaseError("Could not retrieve available interview slots.") from error
    finally:
        cursor.close(); connection.close()


def book_interview_slot(user_id, slot_id):
    connection = get_connection()
    cursor = connection.cursor()
    try:
        cursor.execute(
            """UPDATE scheduled_interviews
               SET user_id = %s, status = 'Scheduled'
               WHERE id = %s AND user_id IS NULL AND status = 'Available'
               AND (interview_date > CURDATE() OR (interview_date = CURDATE() AND interview_time >= CURTIME()))""",
            (user_id, slot_id),
        )
        if cursor.rowcount != 1:
            connection.rollback()
            return "unavailable"
        connection.commit()
        return "scheduled"
    except Error as error:
        connection.rollback()
        raise DatabaseError("Could not schedule the interview.") from error
    finally:
        cursor.close(); connection.close()


def get_user_scheduled_interviews(user_id):
    connection = get_connection()
    cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute(
            """SELECT si.id, si.interview_date, si.interview_time, si.end_time,
                      si.interview_type, si.mode_location, si.status, j.title, j.company
               FROM scheduled_interviews si LEFT JOIN jobs j ON j.id = si.job_id
               WHERE si.user_id = %s ORDER BY si.interview_date DESC, si.interview_time DESC""",
            (user_id,),
        )
        return cursor.fetchall()
    except Error as error:
        raise DatabaseError("Could not retrieve scheduled interviews.") from error
    finally:
        cursor.close(); connection.close()


def cancel_scheduled_interview(user_id, slot_id):
    connection = get_connection(); cursor = connection.cursor()
    try:
        cursor.execute(
            """UPDATE scheduled_interviews SET user_id = NULL, status = 'Available'
               WHERE id = %s AND user_id = %s AND status = 'Scheduled'
               AND (interview_date > CURDATE() OR (interview_date = CURDATE() AND interview_time > CURTIME()))""",
            (slot_id, user_id),
        )
        connection.commit()
        return cursor.rowcount == 1
    except Error as error:
        connection.rollback(); raise DatabaseError("Could not cancel the scheduled interview.") from error
    finally:
        cursor.close(); connection.close()


def get_admin_statistics():
    connection=get_connection(); cursor=connection.cursor(dictionary=True)
    try:
        queries={
            "students":"SELECT COUNT(*) AS value FROM users WHERE role = 'student'",
            "jobs":"SELECT COUNT(*) AS value FROM jobs",
            "applications":"SELECT COUNT(*) AS value FROM job_applications",
            "scheduled":"SELECT COUNT(*) AS value FROM scheduled_interviews WHERE user_id IS NOT NULL",
            "pending":"SELECT COUNT(*) AS value FROM job_applications WHERE status = 'Applied'",
            "upcoming":"SELECT COUNT(*) AS value FROM scheduled_interviews WHERE status IN ('Available','Scheduled') AND interview_date >= CURDATE()",
        }
        result={}
        for key, query in queries.items(): cursor.execute(query); result[key]=cursor.fetchone()["value"]
        return result
    except Error as error:
        raise DatabaseError("Could not retrieve admin statistics.") from error
    finally:
        cursor.close(); connection.close()


def get_admin_students():
    connection=get_connection(); cursor=connection.cursor(dictionary=True)
    try:
        cursor.execute("SELECT id, name, email, role, created_at FROM users ORDER BY created_at DESC, id DESC")
        return cursor.fetchall()
    except Error as error: raise DatabaseError("Could not retrieve students.") from error
    finally: cursor.close(); connection.close()


def create_admin_job(data):
    connection=get_connection(); cursor=connection.cursor()
    try:
        cursor.execute("""INSERT INTO jobs (title,company,location,job_type,salary,description,responsibilities,requirements,skills)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)""", tuple(data[key] for key in ("title","company","location","job_type","salary","description","responsibilities","requirements","skills")))
        connection.commit(); return cursor.lastrowid
    except Error as error: connection.rollback(); raise DatabaseError("Could not create the job.") from error
    finally: cursor.close(); connection.close()


def update_admin_job(job_id, data):
    connection=get_connection(); cursor=connection.cursor()
    try:
        cursor.execute("""UPDATE jobs SET title=%s,company=%s,location=%s,job_type=%s,salary=%s,description=%s,responsibilities=%s,requirements=%s,skills=%s WHERE id=%s""", tuple(data[key] for key in ("title","company","location","job_type","salary","description","responsibilities","requirements","skills"))+(job_id,))
        connection.commit(); return cursor.rowcount == 1
    except Error as error: connection.rollback(); raise DatabaseError("Could not update the job.") from error
    finally: cursor.close(); connection.close()


def delete_admin_job(job_id):
    connection=get_connection(); cursor=connection.cursor()
    try:
        cursor.execute("SELECT (SELECT COUNT(*) FROM job_applications WHERE job_id=%s) + (SELECT COUNT(*) FROM scheduled_interviews WHERE job_id=%s)", (job_id,job_id))
        if cursor.fetchone()[0]: return "in_use"
        cursor.execute("DELETE FROM jobs WHERE id=%s", (job_id,)); connection.commit()
        return "deleted" if cursor.rowcount else "not_found"
    except Error as error: connection.rollback(); raise DatabaseError("Could not delete the job.") from error
    finally: cursor.close(); connection.close()


def get_admin_applications():
    connection=get_connection(); cursor=connection.cursor(dictionary=True)
    try:
        cursor.execute("""SELECT ja.id,ja.status,ja.applied_at,u.name,u.email,j.title,j.company FROM job_applications ja JOIN users u ON u.id=ja.user_id JOIN jobs j ON j.id=ja.job_id ORDER BY ja.applied_at DESC,ja.id DESC""")
        return cursor.fetchall()
    except Error as error: raise DatabaseError("Could not retrieve applications.") from error
    finally: cursor.close(); connection.close()


def update_application_status(application_id, status):
    connection=get_connection(); cursor=connection.cursor()
    try:
        cursor.execute("UPDATE job_applications SET status=%s WHERE id=%s", (status,application_id)); connection.commit(); return cursor.rowcount==1
    except Error as error: connection.rollback(); raise DatabaseError("Could not update application status.") from error
    finally: cursor.close(); connection.close()


def get_admin_slots():
    connection=get_connection(); cursor=connection.cursor(dictionary=True)
    try:
        cursor.execute("""SELECT si.*,j.title,j.company,u.name AS student_name,u.email AS student_email FROM scheduled_interviews si LEFT JOIN jobs j ON j.id=si.job_id LEFT JOIN users u ON u.id=si.user_id ORDER BY si.interview_date DESC,si.interview_time DESC""")
        return cursor.fetchall()
    except Error as error: raise DatabaseError("Could not retrieve interview slots.") from error
    finally: cursor.close(); connection.close()


def create_interview_slot(data):
    connection=get_connection(); cursor=connection.cursor()
    try:
        cursor.execute("SELECT id FROM jobs WHERE id = %s", (data["job_id"],))
        if not cursor.fetchone():
            return "not_found"
        cursor.execute("""SELECT id FROM scheduled_interviews WHERE interview_date=%s AND interview_time < %s AND end_time > %s AND status NOT IN ('Cancelled','Completed')""", (data["interview_date"],data["end_time"],data["interview_time"]))
        if cursor.fetchone(): return "conflict"
        cursor.execute("""INSERT INTO scheduled_interviews (user_id,job_id,interview_date,interview_time,end_time,interview_type,mode_location,capacity,status) VALUES (NULL,%s,%s,%s,%s,%s,%s,%s,'Available')""", (data["job_id"],data["interview_date"],data["interview_time"],data["end_time"],data["interview_type"],data["mode_location"],1))
        connection.commit(); return "created"
    except Error as error: connection.rollback(); raise DatabaseError("Could not create interview slot.") from error
    finally: cursor.close(); connection.close()


def update_slot_status(slot_id, status):
    connection=get_connection(); cursor=connection.cursor()
    try:
        cursor.execute("SELECT user_id, status FROM scheduled_interviews WHERE id = %s", (slot_id,))
        slot = cursor.fetchone()
        if not slot:
            return False
        if status == "Available" and slot[0] is not None:
            return False
        if status == "Scheduled" and slot[0] is None:
            return False
        cursor.execute("UPDATE scheduled_interviews SET status=%s WHERE id=%s", (status,slot_id)); connection.commit(); return cursor.rowcount==1
    except Error as error: connection.rollback(); raise DatabaseError("Could not update interview slot status.") from error
    finally: cursor.close(); connection.close()


def create_user(name, email, password_hash):
    initialize_database()
    connection = get_connection()
    cursor = connection.cursor()
    try:
        cursor.execute(
            """INSERT INTO users (name, email, password_hash, role)
               VALUES (%s, %s, %s, %s)""",
            (name, email, password_hash, "student"),
        )
        connection.commit()
        return cursor.lastrowid
    except Error as error:
        connection.rollback()
        if getattr(error, "errno", None) == 1062:
            raise DuplicateEmailError("An account with this email already exists.") from error
        raise DatabaseError("Could not create the student account.") from error
    finally:
        cursor.close()
        connection.close()


def get_user_by_email(email):
    initialize_database()
    connection = get_connection()
    cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute(
            """SELECT id, name, email, password_hash, role
               FROM users WHERE email = %s""",
            (email,),
        )
        return cursor.fetchone()
    except Error as error:
        raise DatabaseError("Could not retrieve the student account.") from error
    finally:
        cursor.close()
        connection.close()


def get_user_by_id(user_id):
    connection = get_connection()
    cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute(
            "SELECT id, name, email, role, created_at FROM users WHERE id = %s",
            (user_id,),
        )
        return cursor.fetchone()
    except Error as error:
        raise DatabaseError("Could not retrieve the student profile.") from error
    finally:
        cursor.close()
        connection.close()


def get_dashboard_data(user_id):
    connection = get_connection()
    cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute(
            """SELECT COUNT(*) AS total_interviews,
                      AVG(average_score) AS average_score,
                      MAX(average_score) AS best_score,
                      COALESCE(SUM(total_questions), 0) AS total_questions
               FROM interview_attempts
               WHERE user_id = %s""",
            (user_id,),
        )
        statistics = cursor.fetchone()
        cursor.execute(
            """SELECT id, job_role, interview_type, difficulty, average_score, created_at
               FROM interview_attempts
               WHERE user_id = %s
               ORDER BY created_at DESC, id DESC
               LIMIT 5""",
            (user_id,),
        )
        recent_attempts = cursor.fetchall()
        cursor.execute(
            """SELECT job_role, AVG(average_score) AS average_score, COUNT(*) AS attempts
               FROM interview_attempts
               WHERE user_id = %s
               GROUP BY job_role
               ORDER BY average_score DESC, attempts DESC""",
            (user_id,),
        )
        role_performance = cursor.fetchall()
        cursor.execute(
            """SELECT average_score
               FROM interview_attempts
               WHERE user_id = %s
               ORDER BY created_at ASC, id ASC""",
            (user_id,),
        )
        scores = [float(row["average_score"]) for row in cursor.fetchall()]
        return {
            "statistics": statistics,
            "recent_attempts": recent_attempts,
            "role_performance": role_performance,
            "scores": scores,
        }
    except Error as error:
        raise DatabaseError("Could not retrieve dashboard data.") from error
    finally:
        cursor.close()
        connection.close()


def get_saved_attempt(user_id, attempt_id):
    connection = get_connection()
    cursor = connection.cursor(dictionary=True)
    try:
        cursor.execute(
            """SELECT id, job_role, interview_type, difficulty, total_questions,
                      average_score, overall_summary, created_at
               FROM interview_attempts
               WHERE id = %s AND user_id = %s""",
            (attempt_id, user_id),
        )
        attempt = cursor.fetchone()
        if not attempt:
            return None, []
        cursor.execute(
            """SELECT question_number, question, candidate_answer, score, feedback
               FROM interview_responses
               WHERE attempt_id = %s
               ORDER BY question_number ASC""",
            (attempt_id,),
        )
        return attempt, cursor.fetchall()
    except Error as error:
        raise DatabaseError("Could not retrieve the saved interview results.") from error
    finally:
        cursor.close()
        connection.close()


def save_interview_attempt(interview, average_score, overall_summary, user_id):
    initialize_database()
    connection = get_connection()
    cursor = connection.cursor()
    try:
        cursor.execute(
            """INSERT INTO interview_attempts
               (user_id, job_role, interview_type, difficulty, total_questions,
                average_score, overall_summary)
               VALUES (%s, %s, %s, %s, %s, %s, %s)""",
            (
                user_id,
                interview["job_role"],
                interview["interview_type"],
                interview["difficulty"],
                len(interview["questions_asked"]),
                average_score,
                overall_summary,
            ),
        )
        attempt_id = cursor.lastrowid
        response_rows = [
            (
                attempt_id,
                number,
                question,
                interview["candidate_answers"][number - 1],
                interview["scores"][number - 1],
                interview["feedback"][number - 1],
            )
            for number, question in enumerate(interview["questions_asked"], start=1)
        ]
        cursor.executemany(
            """INSERT INTO interview_responses
               (attempt_id, question_number, question, candidate_answer, score, feedback)
               VALUES (%s, %s, %s, %s, %s, %s)""",
            response_rows,
        )
        connection.commit()
        return attempt_id
    except Error as error:
        connection.rollback()
        raise DatabaseError("Could not save the interview results.") from error
    finally:
        cursor.close()
        connection.close()
