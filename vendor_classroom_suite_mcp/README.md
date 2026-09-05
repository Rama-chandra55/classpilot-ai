# 🎓 Classroom Suite MCP

A unified MCP (Model Context Protocol) server for Google Workspace education tools - seamlessly integrate **Google Classroom**, **Google Drive**, and **Google Docs** with AI assistants like Claude.

[![PyPI version](https://badge.fury.io/py/classroom-suite-mcp.svg)](https://pypi.org/project/classroom-suite-mcp/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)

## ✨ Features

| Service | Tools | Description |
|---------|-------|-------------|
| 📚 **Classroom** | 6 | Manage courses, assignments, and submissions |
| 📁 **Drive** | 7 | Create, upload, download, and share files |
| 📝 **Docs** | 4 | Create, read, edit documents and export to PDF |

**17 tools total** - Everything you need for education automation!

## 🚀 Installation

### Using uvx (Recommended)
```bash
uvx classroom-suite-mcp
```

### Using pip
```bash
pip install classroom-suite-mcp
```

### From Source
```bash
git clone https://github.com/anasdevai/classroom-suite-mcp.git
cd classroom-suite-mcp
pip install -e .
```

## 🔐 Google Cloud Setup

Before using this server, you need to set up Google Cloud credentials:

### 1. Create a Google Cloud Project
1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a new project or select an existing one
3. Enable the following APIs:
   - Google Classroom API
   - Google Drive API
   - Google Docs API

### 2. Create OAuth Credentials
1. Go to **APIs & Services > Credentials**
2. Click **Create Credentials > OAuth client ID**
3. Select **Desktop app** as the application type
4. Download the credentials as `credentials.json`

### 3. Configure the Server
Place `credentials.json` in your working directory, or set the path:

```bash
export GOOGLE_CREDENTIALS_PATH=/path/to/credentials.json
```

On first run, a browser window will open for OAuth authentication.

## ⚙️ Configuration

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `GOOGLE_CREDENTIALS_PATH` | `credentials.json` | Path to OAuth credentials file |
| `GOOGLE_TOKEN_PATH` | `token.json` | Path to store auth token |

### Claude Desktop Configuration

Add to your `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "classroom-suite": {
      "command": "uvx",
      "args": ["classroom-suite-mcp"]
    }
  }
}
```

Or with pip installation:

```json
{
  "mcpServers": {
    "classroom-suite": {
      "command": "classroom-suite-mcp"
    }
  }
}
```

## 🛠️ Available Tools

### Google Classroom (6 tools)

| Tool | Description |
|------|-------------|
| `list_courses` | List all enrolled courses |
| `get_course` | Get course details |
| `list_assignments` | List course assignments |
| `get_assignment` | Get assignment details |
| `list_submissions` | List assignment submissions |
| `submit_assignment` | Submit work with attachments |

### Google Drive (7 tools)

| Tool | Description |
|------|-------------|
| `list_files` | List files and folders |
| `search_files` | Search by name or content |
| `create_folder` | Create a new folder |
| `upload_file` | Upload a file |
| `download_file` | Download a file |
| `share_file` | Share with users or publicly |
| `delete_file` | Move to trash or delete |

### Google Docs (4 tools)

| Tool | Description |
|------|-------------|
| `create_doc` | Create a new document |
| `read_doc` | Read document content |
| `update_doc` | Append, prepend, or replace text |
| `export_pdf` | Export as PDF |

## 📖 Usage Examples

### With Claude

```
You: List my Google Classroom courses

Claude: I'll list your courses using the classroom-suite-mcp tools.
[Uses list_courses tool]

Here are your courses:
1. Introduction to Computer Science (ACTIVE)
2. Data Structures and Algorithms (ACTIVE)
...
```

### Common Workflows

**Check assignments and submit:**
```
1. list_courses → Get course ID
2. list_assignments → Find pending assignment
3. create_doc → Create solution document
4. submit_assignment → Submit with Drive file
```

**Organize course materials:**
```
1. create_folder → Create organized folder
2. upload_file → Upload materials
3. share_file → Share with classmates
```

## 🧪 Development

### Running Locally

```bash
# Clone the repository
git clone https://github.com/anasdevai/classroom-suite-mcp.git
cd classroom-suite-mcp

# Install dependencies
pip install -e .

# Run with stdio transport (for MCP clients)
classroom-suite-mcp

# Run with HTTP transport (for testing)
classroom-suite-mcp --transport http --port 8000
```

### Testing with MCP Inspector

```bash
npx @anthropic-ai/mcp-inspector classroom-suite-mcp
```

## 📄 License

MIT License - see [LICENSE](LICENSE) for details.

## 🤝 Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

## 📬 Support

- **Issues:** [GitHub Issues](https://github.com/anasdevai/classroom-suite-mcp/issues)
- **Discussions:** [GitHub Discussions](https://github.com/anasdevai/classroom-suite-mcp/discussions)

---

Made with ❤️ for educators and students
