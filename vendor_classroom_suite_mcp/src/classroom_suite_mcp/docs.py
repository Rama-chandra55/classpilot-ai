"""
Google Docs MCP Tools

Provides tools for creating and managing Google Docs.
"""

import io
from typing import Dict, Any, Optional, List
from googleapiclient.http import MediaIoBaseDownload
from .auth import get_docs_service, get_drive_service


def create_doc(
    title: str,
    content: Optional[str] = None,
    folder_id: Optional[str] = None
) -> Dict[str, Any]:
    """
    Create a new Google Doc.
    
    Args:
        title: Title of the document
        content: Optional initial content to add
        folder_id: Optional folder ID to create the doc in
    
    Returns:
        Document object with id, title, and link
    """
    docs_service = get_docs_service()
    drive_service = get_drive_service()
    
    # Create the document
    doc = docs_service.documents().create(body={"title": title}).execute()
    doc_id = doc.get("documentId")
    
    # Move to folder if specified
    if folder_id:
        drive_service.files().update(
            fileId=doc_id,
            addParents=folder_id,
            removeParents="root",
            fields="id, parents"
        ).execute()
    
    # Add content if provided
    if content:
        requests = [{
            "insertText": {
                "location": {"index": 1},
                "text": content
            }
        }]
        docs_service.documents().batchUpdate(
            documentId=doc_id,
            body={"requests": requests}
        ).execute()
    
    # Get the document link
    file = drive_service.files().get(
        fileId=doc_id,
        fields="webViewLink"
    ).execute()
    
    return {
        "id": doc_id,
        "title": doc.get("title"),
        "web_view_link": file.get("webViewLink"),
    }


def read_doc(document_id: str, include_formatting: bool = False) -> Dict[str, Any]:
    """
    Read the content of a Google Doc.
    
    Args:
        document_id: ID of the document to read
        include_formatting: Whether to include formatting info
    
    Returns:
        Document object with id, title, and content
    """
    docs_service = get_docs_service()
    
    doc = docs_service.documents().get(documentId=document_id).execute()
    
    # Extract text content
    content_parts = []
    body = doc.get("body", {})
    
    for element in body.get("content", []):
        if "paragraph" in element:
            paragraph = element["paragraph"]
            for elem in paragraph.get("elements", []):
                if "textRun" in elem:
                    text = elem["textRun"].get("content", "")
                    content_parts.append(text)
    
    content = "".join(content_parts)
    
    result = {
        "id": doc.get("documentId"),
        "title": doc.get("title"),
        "content": content,
    }
    
    if include_formatting:
        result["body"] = body
    
    return result


def update_doc(
    document_id: str,
    text: str,
    mode: str = "append",
    start_index: Optional[int] = None,
    end_index: Optional[int] = None
) -> Dict[str, Any]:
    """
    Update content in a Google Doc.
    
    Args:
        document_id: ID of the document to update
        text: Text to insert/replace
        mode: "append" (add to end), "prepend" (add to start), or "replace"
        start_index: For replace mode, the start character index
        end_index: For replace mode, the end character index
    
    Returns:
        Updated document info
    """
    docs_service = get_docs_service()
    
    # Get current document to find end index for append
    doc = docs_service.documents().get(documentId=document_id).execute()
    
    # Find the end of the document
    body = doc.get("body", {})
    end_of_doc = 1
    for element in body.get("content", []):
        if "endIndex" in element:
            end_of_doc = max(end_of_doc, element["endIndex"] - 1)
    
    requests = []
    
    if mode == "append":
        requests.append({
            "insertText": {
                "location": {"index": end_of_doc},
                "text": text
            }
        })
    elif mode == "prepend":
        requests.append({
            "insertText": {
                "location": {"index": 1},
                "text": text
            }
        })
    elif mode == "replace":
        if start_index is None or end_index is None:
            raise ValueError("start_index and end_index required for replace mode")
        
        # Delete existing content first
        requests.append({
            "deleteContentRange": {
                "range": {
                    "startIndex": start_index,
                    "endIndex": end_index
                }
            }
        })
        # Then insert new text
        requests.append({
            "insertText": {
                "location": {"index": start_index},
                "text": text
            }
        })
    else:
        raise ValueError(f"Invalid mode: {mode}. Use 'append', 'prepend', or 'replace'")
    
    docs_service.documents().batchUpdate(
        documentId=document_id,
        body={"requests": requests}
    ).execute()
    
    # Return updated doc info
    updated_doc = docs_service.documents().get(documentId=document_id).execute()
    
    return {
        "id": updated_doc.get("documentId"),
        "title": updated_doc.get("title"),
        "updated": True,
    }


def export_pdf(document_id: str) -> Dict[str, Any]:
    """
    Export a Google Doc as PDF.
    
    Args:
        document_id: ID of the document to export
    
    Returns:
        PDF data as base64-encoded string with metadata
    """
    drive_service = get_drive_service()
    docs_service = get_docs_service()
    
    # Get document title
    doc = docs_service.documents().get(documentId=document_id).execute()
    title = doc.get("title", "document")
    
    # Export as PDF
    request = drive_service.files().export_media(
        fileId=document_id,
        mimeType="application/pdf"
    )
    
    fh = io.BytesIO()
    downloader = MediaIoBaseDownload(fh, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    
    import base64
    pdf_content = base64.b64encode(fh.getvalue()).decode("utf-8")
    
    return {
        "id": document_id,
        "title": title,
        "filename": f"{title}.pdf",
        "mime_type": "application/pdf",
        "content_base64": pdf_content,
        "size_bytes": len(fh.getvalue()),
    }
