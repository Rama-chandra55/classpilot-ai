"""
Google OAuth 2.0 Authentication Handler

Handles OAuth flow for Google Classroom, Drive, and Docs APIs.
"""

import os
import pickle
from pathlib import Path
from typing import Optional, List

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build, Resource

# OAuth Scopes for all Google services
SCOPES = [
    # Google Classroom
    "https://www.googleapis.com/auth/classroom.courses.readonly",
    "https://www.googleapis.com/auth/classroom.coursework.me",
    "https://www.googleapis.com/auth/classroom.coursework.students",
    "https://www.googleapis.com/auth/classroom.rosters.readonly",
    # Google Drive
    "https://www.googleapis.com/auth/drive",
    # Google Docs
    "https://www.googleapis.com/auth/documents",
]


class GoogleAuth:
    """Manages Google OAuth authentication and service clients."""
    
    def __init__(
        self,
        credentials_path: Optional[str] = None,
        token_path: Optional[str] = None,
        scopes: Optional[List[str]] = None,
    ):
        """
        Initialize the Google Auth handler.
        
        Args:
            credentials_path: Path to OAuth credentials.json file
            token_path: Path to store/load the token
            scopes: OAuth scopes to request
        """
        self.credentials_path = credentials_path or os.getenv(
            "GOOGLE_CREDENTIALS_PATH", "credentials.json"
        )
        self.token_path = token_path or os.getenv(
            "GOOGLE_TOKEN_PATH", "token.json"
        )
        self.scopes = scopes or SCOPES
        self._creds: Optional[Credentials] = None
        self._classroom_service: Optional[Resource] = None
        self._drive_service: Optional[Resource] = None
        self._docs_service: Optional[Resource] = None
    
    def authenticate(self) -> Credentials:
        """
        Authenticate with Google and return credentials.
        
        If valid credentials exist, they are loaded from token file.
        Otherwise, initiates OAuth flow.
        
        Returns:
            Google OAuth Credentials object
        """
        creds = None
        token_path = Path(self.token_path)
        
        # Check for existing token
        if token_path.exists():
            # Try pickle format first (legacy)
            if self.token_path.endswith('.pickle'):
                with open(token_path, 'rb') as token:
                    creds = pickle.load(token)
            else:
                creds = Credentials.from_authorized_user_file(
                    str(token_path), self.scopes
                )
        
        # If no valid credentials, refresh or re-authenticate
        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                if not Path(self.credentials_path).exists():
                    raise FileNotFoundError(
                        f"Credentials file not found: {self.credentials_path}\n"
                        "Please download OAuth credentials from Google Cloud Console "
                        "and save as 'credentials.json'"
                    )
                
                flow = InstalledAppFlow.from_client_secrets_file(
                    self.credentials_path, self.scopes
                )
                creds = flow.run_local_server(port=0)
            
            # Save the credentials for future runs
            with open(token_path, 'w') as token:
                token.write(creds.to_json())
        
        self._creds = creds
        return creds
    
    @property
    def credentials(self) -> Credentials:
        """Get or create credentials."""
        if not self._creds:
            self.authenticate()
        return self._creds
    
    @property
    def classroom(self) -> Resource:
        """Get Google Classroom service client."""
        if not self._classroom_service:
            self._classroom_service = build(
                'classroom', 'v1', credentials=self.credentials
            )
        return self._classroom_service
    
    @property
    def drive(self) -> Resource:
        """Get Google Drive service client."""
        if not self._drive_service:
            self._drive_service = build(
                'drive', 'v3', credentials=self.credentials
            )
        return self._drive_service
    
    @property
    def docs(self) -> Resource:
        """Get Google Docs service client."""
        if not self._docs_service:
            self._docs_service = build(
                'docs', 'v1', credentials=self.credentials
            )
        return self._docs_service


# Global auth instance
_auth: Optional[GoogleAuth] = None


def get_auth() -> GoogleAuth:
    """Get the global GoogleAuth instance."""
    global _auth
    if _auth is None:
        _auth = GoogleAuth()
    return _auth


def get_classroom_service() -> Resource:
    """Get the Google Classroom service."""
    return get_auth().classroom


def get_drive_service() -> Resource:
    """Get the Google Drive service."""
    return get_auth().drive


def get_docs_service() -> Resource:
    """Get the Google Docs service."""
    return get_auth().docs
