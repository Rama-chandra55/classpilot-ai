"""
Classroom Suite MCP Server

A unified MCP server for Google Classroom, Drive, and Docs integration.
"""

import os
import sys
import argparse
from typing import Optional
from dotenv import load_dotenv
from fastmcp import FastMCP

# Import all tool modules
from . import classroom
from . import drive
from . import docs

# Load environment variables
load_dotenv()

# Initialize the MCP server
mcp = FastMCP(
    name="classroom-suite-mcp",
    instructions="""
    Classroom Suite MCP provides unified access to Google Workspace education tools.
    
    Available capabilities:
    - Google Classroom: Manage courses, assignments, and submissions
    - Google Drive: Create, upload, download, and share files
    - Google Docs: Create, read, edit documents and export to PDF
    
    Before using any tools, ensure OAuth authentication is complete.
    """
)


# ==================== CLASSROOM TOOLS ====================

@mcp.tool()
def list_courses(page_size: int = 20, course_states: Optional[str] = None) -> list:
    """
    List all courses the authenticated user is enrolled in.
    
    Args:
        page_size: Maximum number of courses to return (default: 20)
        course_states: Comma-separated states: ACTIVE, ARCHIVED, PROVISIONED, DECLINED, SUSPENDED
    
    Returns:
        List of courses with id, name, section, and state
    """
    states = course_states.split(",") if course_states else None
    return classroom.list_courses(page_size=page_size, course_states=states)


@mcp.tool()
def get_course(course_id: str) -> dict:
    """
    Get detailed information about a specific course.
    
    Args:
        course_id: The ID of the course to retrieve
    
    Returns:
        Course details including name, description, and links
    """
    return classroom.get_course(course_id)


@mcp.tool()
def list_assignments(course_id: str, page_size: int = 20) -> list:
    """
    List all assignments for a specific course.
    
    Args:
        course_id: The ID of the course
        page_size: Maximum number of assignments to return
    
    Returns:
        List of assignments with id, title, due date, and points
    """
    return classroom.list_assignments(course_id, page_size=page_size)


@mcp.tool()
def get_assignment(course_id: str, assignment_id: str) -> dict:
    """
    Get detailed information about a specific assignment.
    
    Args:
        course_id: The ID of the course
        assignment_id: The ID of the assignment
    
    Returns:
        Assignment details including description, materials, and due date
    """
    return classroom.get_assignment(course_id, assignment_id)


@mcp.tool()
def list_submissions(
    course_id: str,
    assignment_id: str,
    page_size: int = 20,
    states: Optional[str] = None
) -> list:
    """
    List all submissions for an assignment.
    
    Args:
        course_id: The ID of the course
        assignment_id: The ID of the assignment
        page_size: Maximum number of submissions to return
        states: Comma-separated states: NEW, CREATED, TURNED_IN, RETURNED
    
    Returns:
        List of submissions with state, grade, and links
    """
    state_list = states.split(",") if states else None
    return classroom.list_submissions(
        course_id, assignment_id, 
        page_size=page_size, 
        states=state_list
    )


@mcp.tool()
def submit_assignment(
    course_id: str,
    assignment_id: str,
    submission_id: str,
    drive_file_ids: Optional[str] = None,
    link_urls: Optional[str] = None
) -> dict:
    """
    Submit work to an assignment.
    
    Args:
        course_id: The ID of the course
        assignment_id: The ID of the assignment  
        submission_id: The ID of your student submission
        drive_file_ids: Comma-separated Google Drive file IDs to attach
        link_urls: Comma-separated URLs to attach as links
    
    Returns:
        Updated submission with state and timestamp
    """
    attachments = []
    if drive_file_ids:
        for file_id in drive_file_ids.split(","):
            attachments.append({"drive_file_id": file_id.strip()})
    if link_urls:
        for url in link_urls.split(","):
            attachments.append({"link_url": url.strip()})
    
    return classroom.submit_assignment(
        course_id, assignment_id, submission_id,
        attachments=attachments if attachments else None
    )


# ==================== DRIVE TOOLS ====================

@mcp.tool()
def list_files(
    folder_id: Optional[str] = None,
    page_size: int = 20,
    file_type: Optional[str] = None
) -> list:
    """
    List files and folders in Google Drive.
    
    Args:
        folder_id: ID of folder to list (None for all files)
        page_size: Maximum number of files to return
        file_type: Filter by MIME type (e.g., "application/pdf")
    
    Returns:
        List of files with id, name, type, and links
    """
    return drive.list_files(
        folder_id=folder_id,
        page_size=page_size,
        file_type=file_type
    )


@mcp.tool()
def search_files(query: str, page_size: int = 20) -> list:
    """
    Search for files by name or content.
    
    Args:
        query: Search query string
        page_size: Maximum number of results
    
    Returns:
        List of matching files
    """
    return drive.search_files(query, page_size=page_size)


@mcp.tool()
def create_folder(name: str, parent_id: Optional[str] = None) -> dict:
    """
    Create a new folder in Google Drive.
    
    Args:
        name: Name of the folder
        parent_id: ID of parent folder (None for root)
    
    Returns:
        Created folder with id and link
    """
    return drive.create_folder(name, parent_id=parent_id)


@mcp.tool()
def upload_file(
    name: str,
    content: str,
    mime_type: str = "text/plain",
    parent_id: Optional[str] = None,
    is_base64: bool = False
) -> dict:
    """
    Upload a file to Google Drive.
    
    Args:
        name: Filename
        content: File content (text or base64 for binary)
        mime_type: MIME type of the file
        parent_id: Parent folder ID
        is_base64: True if content is base64-encoded
    
    Returns:
        Uploaded file with id and link
    """
    return drive.upload_file(
        name, content,
        mime_type=mime_type,
        parent_id=parent_id,
        is_base64=is_base64
    )


@mcp.tool()
def download_file(file_id: str, as_base64: bool = False) -> dict:
    """
    Download a file from Google Drive.
    
    Args:
        file_id: ID of the file
        as_base64: Return content as base64 (for binary)
    
    Returns:
        File with id, name, and content
    """
    return drive.download_file(file_id, as_base64=as_base64)


@mcp.tool()
def share_file(
    file_id: str,
    email: Optional[str] = None,
    role: str = "reader",
    anyone: bool = False
) -> dict:
    """
    Share a file with users or make it public.
    
    Args:
        file_id: ID of the file to share
        email: Email address to share with
        role: Permission level (reader, writer, commenter)
        anyone: Make accessible to anyone with link
    
    Returns:
        File link after sharing
    """
    return drive.share_file(
        file_id,
        email=email,
        role=role,
        anyone=anyone
    )


@mcp.tool()
def delete_file(file_id: str, permanent: bool = False) -> dict:
    """
    Delete a file (move to trash or permanent).
    
    Args:
        file_id: ID of the file
        permanent: Permanently delete if True
    
    Returns:
        Deletion confirmation
    """
    return drive.delete_file(file_id, permanent=permanent)


# ==================== DOCS TOOLS ====================

@mcp.tool()
def create_doc(
    title: str,
    content: Optional[str] = None,
    folder_id: Optional[str] = None
) -> dict:
    """
    Create a new Google Doc.
    
    Args:
        title: Document title
        content: Optional initial text content
        folder_id: Folder to create the doc in
    
    Returns:
        Document with id, title, and link
    """
    return docs.create_doc(title, content=content, folder_id=folder_id)


@mcp.tool()
def read_doc(document_id: str) -> dict:
    """
    Read the content of a Google Doc.
    
    Args:
        document_id: ID of the document
    
    Returns:
        Document with id, title, and text content
    """
    return docs.read_doc(document_id)


@mcp.tool()
def update_doc(
    document_id: str,
    text: str,
    mode: str = "append"
) -> dict:
    """
    Update content in a Google Doc.
    
    Args:
        document_id: ID of the document
        text: Text to add
        mode: "append" (end), "prepend" (start), or "replace"
    
    Returns:
        Update confirmation
    """
    return docs.update_doc(document_id, text, mode=mode)


@mcp.tool()
def export_pdf(document_id: str) -> dict:
    """
    Export a Google Doc as PDF.
    
    Args:
        document_id: ID of the document
    
    Returns:
        PDF as base64 with filename and size
    """
    return docs.export_pdf(document_id)


def main():
    """Main entry point for the MCP server."""
    parser = argparse.ArgumentParser(
        description="Classroom Suite MCP Server"
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "http"],
        default="stdio",
        help="Transport mode (default: stdio)"
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Port for HTTP transport (default: 8000)"
    )
    
    args = parser.parse_args()
    
    print(f"🎓 Classroom Suite MCP Server v1.0.0")
    print(f"📚 Tools: 17 (Classroom: 6, Drive: 7, Docs: 4)")
    print(f"🚀 Starting with {args.transport} transport...")
    
    if args.transport == "http":
        mcp.run(transport="http", port=args.port)
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
