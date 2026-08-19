# ClassPilot AI 🎓

**ClassPilot AI** is an MCP-based AI study assistant that connects Google Classroom with AI assistants like Claude.

It allows students to navigate their Classroom, find modules and study materials, read supported materials, and ask the AI to explain, summarize, or generate questions from them.

## ✨ Current Features

* 🔗 Google Classroom integration
* 📚 List classes and modules
* 📑 List study materials
* 🔎 Search across Classroom
* 📖 Read Google Docs, Slides, Sheets
* 📊 Read uploaded `.pptx` files
* 📝 Generate summaries, explanations, and exam questions from study materials
* ⏰ View upcoming assignment deadlines

### Current Workflow

**Google Classroom → ClassPilot AI → Claude → Study Material → Answer**

## 🛠️ Tech Stack

Python • FastMCP • MCP • Google Classroom API • Google Drive API • Google OAuth 2.0 • python-pptx • Claude • Cloudflare Tunnel

## 🚧 Current Status

The core Classroom study-assistant workflow is **working**.

Currently tested:

* ✅ Multiple Google Classroom courses
* ✅ Module/topic navigation
* ✅ Study-material retrieval
* ✅ PPTX content extraction
* ✅ AI-generated answers from PPT content
* ✅ Claude + MCP integration

The current implementation is primarily designed for a **single authenticated Google account**.

## 🔮 Future Implementation

* PDF support
* DOCX / XLSX support
* Image and diagram understanding
* Improved study and revision tools
* Automatic quizzes and exam preparation
* Multi-user Google OAuth
* Production deployment

## 📌 Note

ClassPilot AI is currently focused on **reading and studying Classroom materials**. Assignment submission is not part of the current implementation.

## 📜 License

MIT License
