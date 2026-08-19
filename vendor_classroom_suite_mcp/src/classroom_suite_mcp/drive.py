"""
Google Drive MCP Tools

Provides tools for file and folder management in Google Drive.
"""

import io
import base64
from typing import List, Dict, Any, Optional
from googleapiclient.http import MediaIoBaseUpload, MediaIoBaseDownload
from .auth import get_drive_service


def list_files(
    folder_id: Optional[str] = None,
    page_size: int = 20,
    file_type: Optional[str] = None,
    order_by: str = "modifiedTime desc"
) -> List[Dict[str, Any]]:
    """
    List files and folders in Google Drive.
    
    Args:
        folder_id: ID of folder to list (None for root/all files)
        page_size: Maximum number of files to return
        file_type: Filter by MIME type (e.g., "application/pdf")
        order_by: Sort order (e.g., "modifiedTime desc", "name")
    
    Returns:
        List of file objects with id, name, type, and other details
    """
    service = get_drive_service()
    
    query_parts = ["trashed = false"]
    if folder_id:
        query_parts.append(f"'{folder_id}' in parents")
    if file_type:
        query_parts.append(f"mimeType = '{file_type}'")
    
    query = " and ".join(query_parts)
    
    results = service.files().list(
        q=query,
        pageSize=page_size,
        orderBy=order_by,
        fields="files(id, name, mimeType, size, createdTime, modifiedTime, webViewLink, parents)"
    ).execute()
    
    files = results.get("files", [])
    
    return [
        {
            "id": f.get("id"),
            "name": f.get("name"),
            "mime_type": f.get("mimeType"),
            "size": f.get("size"),
            "created_time": f.get("createdTime"),
            "modified_time": f.get("modifiedTime"),
            "web_view_link": f.get("webViewLink"),
            "parent_ids": f.get("parents", []),
            "is_folder": f.get("mimeType") == "application/vnd.google-apps.folder",
        }
        for f in files
    ]


def search_files(
    query: str,
    page_size: int = 20,
    include_folders: bool = False
) -> List[Dict[str, Any]]:
    """
    Search for files by name or content.
    
    Args:
        query: Search query (searches name and content)
        page_size: Maximum number of files to return
        include_folders: Whether to include folders in results
    
    Returns:
        List of matching file objects
    """
    service = get_drive_service()
    
    # Build the query
    search_query = f"fullText contains '{query}' and trashed = false"
    if not include_folders:
        search_query += " and mimeType != 'application/vnd.google-apps.folder'"
    
    results = service.files().list(
        q=search_query,
        pageSize=page_size,
        fields="files(id, name, mimeType, size, modifiedTime, webViewLink)"
    ).execute()
    
    files = results.get("files", [])
    
    return [
        {
            "id": f.get("id"),
            "name": f.get("name"),
            "mime_type": f.get("mimeType"),
            "size": f.get("size"),
            "modified_time": f.get("modifiedTime"),
            "web_view_link": f.get("webViewLink"),
        }
        for f in files
    ]


def create_folder(
    name: str,
    parent_id: Optional[str] = None
) -> Dict[str, Any]:
    """
    Create a new folder in Google Drive.
    
    Args:
        name: Name of the folder
        parent_id: ID of parent folder (None for root)
    
    Returns:
        Created folder object with id and link
    """
    service = get_drive_service()
    
    file_metadata = {
        "name": name,
        "mimeType": "application/vnd.google-apps.folder"
    }
    if parent_id:
        file_metadata["parents"] = [parent_id]
    
    folder = service.files().create(
        body=file_metadata,
        fields="id, name, webViewLink"
    ).execute()
    
    return {
        "id": folder.get("id"),
        "name": folder.get("name"),
        "web_view_link": folder.get("webViewLink"),
    }


def upload_file(
    name: str,
    content: str,
    mime_type: str = "text/plain",
    parent_id: Optional[str] = None,
    is_base64: bool = False
) -> Dict[str, Any]:
    """
    Upload a file to Google Drive.
    
    Args:
        name: Name of the file
        content: File content (text or base64-encoded binary)
        mime_type: MIME type of the file
        parent_id: ID of parent folder (None for root)
        is_base64: Whether content is base64-encoded
    
    Returns:
        Uploaded file object with id and link
    """
    service = get_drive_service()
    
    file_metadata = {"name": name}
    if parent_id:
        file_metadata["parents"] = [parent_id]
    
    # Decode if base64
    if is_base64:
        file_content = base64.b64decode(content)
    else:
        file_content = content.encode("utf-8")
    
    media = MediaIoBaseUpload(
        io.BytesIO(file_content),
        mimetype=mime_type,
        resumable=True
    )
    
    file = service.files().create(
        body=file_metadata,
        media_body=media,
        fields="id, name, webViewLink, size"
    ).execute()
    
    return {
        "id": file.get("id"),
        "name": file.get("name"),
        "web_view_link": file.get("webViewLink"),
        "size": file.get("size"),
    }


def download_file(file_id: str, as_base64: bool = False) -> Dict[str, Any]:
    """
    Download a file from Google Drive.
    
    Args:
        file_id: ID of the file to download
        as_base64: Return content as base64 string (for binary files)
    
    Returns:
        File object with id, name, and content
    """
    service = get_drive_service()
    
    # Get file metadata
    file_meta = service.files().get(
        fileId=file_id,
        fields="id, name, mimeType"
    ).execute()
    
    mime_type = file_meta.get("mimeType", "")
    
    # Handle Google Docs/Sheets/Slides differently
    google_export_types = {
        "application/vnd.google-apps.document": "text/plain",
        "application/vnd.google-apps.spreadsheet": "text/csv",
        "application/vnd.google-apps.presentation": "application/pdf",
    }
    
    if mime_type in google_export_types:
        request = service.files().export_media(
            fileId=file_id,
            mimeType=google_export_types[mime_type]
        )
    else:
        request = service.files().get_media(fileId=file_id)
    
    fh = io.BytesIO()
    downloader = MediaIoBaseDownload(fh, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    
    content = fh.getvalue()
    
    if as_base64:
        content_str = base64.b64encode(content).decode("utf-8")
    else:
        try:
            content_str = content.decode("utf-8")
        except UnicodeDecodeError:
            content_str = base64.b64encode(content).decode("utf-8")
    
    return {
        "id": file_meta.get("id"),
        "name": file_meta.get("name"),
        "mime_type": mime_type,
        "content": content_str,
    }


def share_file(
    file_id: str,
    email: Optional[str] = None,
    role: str = "reader",
    anyone: bool = False
) -> Dict[str, Any]:
    """
    Share a file with specific users or make it publicly accessible.
    
    Args:
        file_id: ID of the file to share
        email: Email address of the user to share with
        role: Permission level: "reader", "writer", "commenter"
        anyone: If True, make file accessible to anyone with link
    
    Returns:
        Permission object with id and link
    """
    service = get_drive_service()
    
    if anyone:
        permission = {
            "type": "anyone",
            "role": role,
        }
    elif email:
        permission = {
            "type": "user",
            "role": role,
            "emailAddress": email,
        }
    else:
        raise ValueError("Either 'email' or 'anyone=True' must be specified")
    
    result = service.permissions().create(
        fileId=file_id,
        body=permission,
        fields="id"
    ).execute()
    
    # Get the updated file info
    file = service.files().get(
        fileId=file_id,
        fields="id, name, webViewLink"
    ).execute()
    
    return {
        "permission_id": result.get("id"),
        "file_id": file.get("id"),
        "file_name": file.get("name"),
        "web_view_link": file.get("webViewLink"),
    }


def delete_file(file_id: str, permanent: bool = False) -> Dict[str, str]:
    """
    Delete a file (move to trash or permanently delete).
    
    Args:
        file_id: ID of the file to delete
        permanent: If True, permanently delete (cannot be recovered)
    
    Returns:
        Confirmation message
    """
    service = get_drive_service()
    
    if permanent:
        service.files().delete(fileId=file_id).execute()
        return {"status": "deleted", "file_id": file_id, "permanent": True}
    else:
        service.files().update(
            fileId=file_id,
            body={"trashed": True}
        ).execute()
        return {"status": "trashed", "file_id": file_id, "permanent": False}
