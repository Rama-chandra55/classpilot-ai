"""
Google Classroom MCP Tools

Provides tools for managing courses, assignments, and submissions.
"""

from typing import List, Dict, Any, Optional
from .auth import get_classroom_service


def list_courses(
    page_size: int = 20,
    course_states: Optional[List[str]] = None
) -> List[Dict[str, Any]]:
    """
    List all courses the authenticated user is enrolled in.
    
    Args:
        page_size: Maximum number of courses to return (default: 20)
        course_states: Filter by state: ACTIVE, ARCHIVED, PROVISIONED, DECLINED, SUSPENDED
    
    Returns:
        List of course objects with id, name, section, and other details
    """
    service = get_classroom_service()
    
    params = {"pageSize": page_size}
    if course_states:
        params["courseStates"] = course_states
    
    results = service.courses().list(**params).execute()
    courses = results.get("courses", [])
    
    return [
        {
            "id": c.get("id"),
            "name": c.get("name"),
            "section": c.get("section"),
            "description": c.get("descriptionHeading"),
            "room": c.get("room"),
            "owner_id": c.get("ownerId"),
            "state": c.get("courseState"),
            "enrollment_code": c.get("enrollmentCode"),
            "alternate_link": c.get("alternateLink"),
        }
        for c in courses
    ]


def get_course(course_id: str) -> Dict[str, Any]:
    """
    Get detailed information about a specific course.
    
    Args:
        course_id: The ID of the course to retrieve
    
    Returns:
        Course object with full details
    """
    service = get_classroom_service()
    course = service.courses().get(id=course_id).execute()
    
    return {
        "id": course.get("id"),
        "name": course.get("name"),
        "section": course.get("section"),
        "description": course.get("descriptionHeading"),
        "description_body": course.get("description"),
        "room": course.get("room"),
        "owner_id": course.get("ownerId"),
        "state": course.get("courseState"),
        "enrollment_code": course.get("enrollmentCode"),
        "alternate_link": course.get("alternateLink"),
        "created_time": course.get("creationTime"),
        "update_time": course.get("updateTime"),
    }


def list_assignments(
    course_id: str,
    page_size: int = 20,
    order_by: str = "dueDate desc"
) -> List[Dict[str, Any]]:
    """
    List all assignments (coursework) for a specific course.
    
    Args:
        course_id: The ID of the course
        page_size: Maximum number of assignments to return
        order_by: Sort order (e.g., "dueDate desc", "updateTime desc")
    
    Returns:
        List of assignment objects
    """
    service = get_classroom_service()
    
    results = service.courses().courseWork().list(
        courseId=course_id,
        pageSize=page_size,
        orderBy=order_by
    ).execute()
    
    coursework = results.get("courseWork", [])
    
    return [
        {
            "id": cw.get("id"),
            "title": cw.get("title"),
            "description": cw.get("description"),
            "state": cw.get("state"),
            "work_type": cw.get("workType"),
            "max_points": cw.get("maxPoints"),
            "due_date": cw.get("dueDate"),
            "due_time": cw.get("dueTime"),
            "alternate_link": cw.get("alternateLink"),
            "creation_time": cw.get("creationTime"),
            "update_time": cw.get("updateTime"),
            "materials": cw.get("materials", []),
        }
        for cw in coursework
    ]


def get_assignment(course_id: str, assignment_id: str) -> Dict[str, Any]:
    """
    Get detailed information about a specific assignment.
    
    Args:
        course_id: The ID of the course
        assignment_id: The ID of the assignment (coursework)
    
    Returns:
        Assignment object with full details
    """
    service = get_classroom_service()
    
    cw = service.courses().courseWork().get(
        courseId=course_id,
        id=assignment_id
    ).execute()
    
    return {
        "id": cw.get("id"),
        "title": cw.get("title"),
        "description": cw.get("description"),
        "state": cw.get("state"),
        "work_type": cw.get("workType"),
        "max_points": cw.get("maxPoints"),
        "due_date": cw.get("dueDate"),
        "due_time": cw.get("dueTime"),
        "alternate_link": cw.get("alternateLink"),
        "creation_time": cw.get("creationTime"),
        "update_time": cw.get("updateTime"),
        "materials": cw.get("materials", []),
        "submission_modification_mode": cw.get("submissionModificationMode"),
    }


def list_submissions(
    course_id: str,
    assignment_id: str,
    page_size: int = 20,
    states: Optional[List[str]] = None
) -> List[Dict[str, Any]]:
    """
    List all submissions for an assignment.
    
    Args:
        course_id: The ID of the course
        assignment_id: The ID of the assignment
        page_size: Maximum number of submissions to return
        states: Filter by state: NEW, CREATED, TURNED_IN, RETURNED, RECLAIMED_BY_STUDENT
    
    Returns:
        List of submission objects
    """
    service = get_classroom_service()
    
    params = {
        "courseId": course_id,
        "courseWorkId": assignment_id,
        "pageSize": page_size,
    }
    if states:
        params["states"] = states
    
    results = service.courses().courseWork().studentSubmissions().list(
        **params
    ).execute()
    
    submissions = results.get("studentSubmissions", [])
    
    return [
        {
            "id": s.get("id"),
            "user_id": s.get("userId"),
            "state": s.get("state"),
            "late": s.get("late", False),
            "assigned_grade": s.get("assignedGrade"),
            "draft_grade": s.get("draftGrade"),
            "alternate_link": s.get("alternateLink"),
            "creation_time": s.get("creationTime"),
            "update_time": s.get("updateTime"),
            "assignment_submission": s.get("assignmentSubmission"),
        }
        for s in submissions
    ]


def submit_assignment(
    course_id: str,
    assignment_id: str,
    submission_id: str,
    attachments: Optional[List[Dict[str, str]]] = None
) -> Dict[str, Any]:
    """
    Submit work to an assignment.
    
    Args:
        course_id: The ID of the course
        assignment_id: The ID of the assignment
        submission_id: The ID of the student submission
        attachments: Optional list of attachments, each with:
            - drive_file_id: ID of a Google Drive file
            - link_url: URL of a link attachment
    
    Returns:
        Updated submission object
    """
    service = get_classroom_service()
    
    # First, add attachments if provided
    if attachments:
        for attachment in attachments:
            add_request = {}
            
            if "drive_file_id" in attachment:
                add_request["addAttachments"] = [{
                    "driveFile": {"id": attachment["drive_file_id"]}
                }]
            elif "link_url" in attachment:
                add_request["addAttachments"] = [{
                    "link": {"url": attachment["link_url"]}
                }]
            
            if add_request:
                service.courses().courseWork().studentSubmissions().modifyAttachments(
                    courseId=course_id,
                    courseWorkId=assignment_id,
                    id=submission_id,
                    body=add_request
                ).execute()
    
    # Then turn in the submission
    service.courses().courseWork().studentSubmissions().turnIn(
        courseId=course_id,
        courseWorkId=assignment_id,
        id=submission_id,
        body={}
    ).execute()
    
    # Get and return the updated submission
    result = service.courses().courseWork().studentSubmissions().get(
        courseId=course_id,
        courseWorkId=assignment_id,
        id=submission_id
    ).execute()
    
    return {
        "id": result.get("id"),
        "state": result.get("state"),
        "late": result.get("late", False),
        "alternate_link": result.get("alternateLink"),
        "update_time": result.get("updateTime"),
    }


# ── ClassPilot AI additions (additive only — existing functions untouched) ────

def attach_submission_files(
    course_id: str,
    assignment_id: str,
    submission_id: str,
    attachments: Optional[List[Dict[str, str]]] = None,
) -> Dict[str, Any]:
    """
    Attach Drive files to a student submission WITHOUT turning it in.

    This is the first half of the two-step submission flow required by
    Features 4/5. The caller must call turn_in_submission() separately
    after the user confirms.

    Args:
        course_id: The ID of the course.
        assignment_id: The ID of the assignment (coursework).
        submission_id: The ID of the student submission.
        attachments: List of dicts with 'drive_file_id' or 'link_url'.

    Returns:
        The updated submission object.
    """
    service = get_classroom_service()

    if attachments:
        for attachment in attachments:
            add_request = {}
            if "drive_file_id" in attachment:
                add_request["addAttachments"] = [
                    {"driveFile": {"id": attachment["drive_file_id"]}}
                ]
            elif "link_url" in attachment:
                add_request["addAttachments"] = [
                    {"link": {"url": attachment["link_url"]}}
                ]
            if add_request:
                service.courses().courseWork().studentSubmissions().modifyAttachments(
                    courseId=course_id,
                    courseWorkId=assignment_id,
                    id=submission_id,
                    body=add_request,
                ).execute()

    result = service.courses().courseWork().studentSubmissions().get(
        courseId=course_id,
        courseWorkId=assignment_id,
        id=submission_id,
    ).execute()

    return {
        "id": result.get("id"),
        "state": result.get("state"),
        "late": result.get("late", False),
        "alternate_link": result.get("alternateLink"),
        "update_time": result.get("updateTime"),
    }


def turn_in_submission(
    course_id: str,
    assignment_id: str,
    submission_id: str,
) -> Dict[str, Any]:
    """
    Turn in a student submission that has already had files attached.

    This is the second half of the two-step submission flow (Feature 5).
    It calls the Classroom API turnIn endpoint and returns the final
    submission state.

    Args:
        course_id: The ID of the course.
        assignment_id: The ID of the assignment (coursework).
        submission_id: The ID of the student submission.

    Returns:
        The final submission object with updated state.
    """
    service = get_classroom_service()

    service.courses().courseWork().studentSubmissions().turnIn(
        courseId=course_id,
        courseWorkId=assignment_id,
        id=submission_id,
        body={},
    ).execute()

    result = service.courses().courseWork().studentSubmissions().get(
        courseId=course_id,
        courseWorkId=assignment_id,
        id=submission_id,
    ).execute()

    return {
        "id": result.get("id"),
        "state": result.get("state"),
        "late": result.get("late", False),
        "alternate_link": result.get("alternateLink"),
        "update_time": result.get("updateTime"),
    }
