# Browser Use Local Bridge for n8n

This is a local bridge service that enables n8n to communicate with the Browser Use Python library. It mimics the Browser Use Cloud API endpoints but runs locally, allowing you to execute browser automation tasks without relying on the cloud service.

## Features

- Compatible with the Browser Use Cloud API endpoints
- Supports both OpenAI and Anthropic language models
- Provides task management (run, pause, resume, stop)
- Exposes status tracking and result retrieval

## Prerequisites

- Python 3.10 or higher
- pip (Python package manager)
- Browser Use Python library
- API keys for OpenAI or Anthropic (depending on which LLM you want to use)

## Installation

1. Clone this repository:
   ```bash
   git clone https://github.com/draphonix/browser-n8n-local.git
   cd browser-n8n-local
   ```

2. Create a virtual environment (recommended):
   ```bash
   python -m venv venv
   . venv/bin/activate  # On Windows: venv\Scripts\activate
   ```

3. Install the required dependencies:
   ```bash
   pip install -r requirements.txt
   ```

4. Set up environment variables:
   ```bash
   cp .env-example .env
   ```
   Then edit the `.env` file to add your OpenAI and/or Anthropic API keys.

## Running the Service

1. Start the FastAPI server:
   ```bash
   python app.py
   ```

2. The server will start at http://localhost:8000 by default.

3. You can access the API documentation at http://localhost:8000/docs

## API Endpoints

| Method | Endpoint                      | Description                  |
|--------|-------------------------------|------------------------------|
| POST   | /api/v1/run-task              | Start a new browser task     |
| GET    | /api/v1/task/{task_id}        | Get task details             |
| GET    | /api/v1/task/{task_id}/status | Get task status              |
| PUT    | /api/v1/stop-task/{task_id}   | Stop a running task          |
| PUT    | /api/v1/pause-task/{task_id}  | Pause a running task         |
| PUT    | /api/v1/resume-task/{task_id} | Resume a paused task         |
| GET    | /api/v1/list-tasks            | List all tasks               |
| GET    | /live/{task_id}               | Live HTML view of a task     |
| GET    | /api/v1/ping                  | Health check                 |
| GET    | /api/v1/providers             | List AI providers            |
| GET    | /api/v1/browser-config        | Get browser configuration    |
| POST   | /api/v1/douyin/channel-videos | Get video list from channel  |
| GET    | /api/v1/douyin/stream-results/{task_id} | Poll background scrape results |
| POST   | /api/v1/douyin/video-detail   | Get single video detail      |
| POST   | /api/v1/scrape-douyin-channel | Start sync full scrape task  |
| GET    | /api/v1/scrape-task/{task_id} | Get status of full scrape    |

### Detailed API Descriptions

#### 1. Douyin Scraper APIs
*   **`POST /api/v1/douyin/channel-videos`**: Lấy danh sách video từ trang chủ của kênh (trả về ngay lập tức). Đồng thời tạo một task chạy ngầm (Phase 2) nếu hệ thống được cấu hình theo Luồng 2.
*   **`GET /api/v1/douyin/stream-results/{task_id}`**: Dùng để polling (kiểm tra định kỳ) kết quả từ quá trình chạy ngầm Phase 2. Trả về tiến độ và danh sách stream URLs.
*   **`POST /api/v1/douyin/video-detail`**: Truyền vào URL của một video độc lập để bắt luồng video/audio và lấy thông tin chi tiết ngay lập tức.
*   **`POST /api/v1/scrape-douyin-channel`**: Chạy đồng bộ trọn gói quá trình cào kênh (cào danh sách sau đó tự động chui vào cào chi tiết). Hàm này trả về `task_id`.
*   **`GET /api/v1/scrape-task/{task_id}`**: Lấy kết quả và trạng thái của tiến trình cào đồng bộ trọn gói ở trên.

#### 2. Browser Use Task Management APIs (AI Agent)
*   **`POST /api/v1/run-task`**: Khởi chạy một AI Agent mới với một chỉ thị đầu vào (task) và cấu hình mô hình AI mong muốn (OpenAI, Anthropic, Mistral...).
*   **`GET /api/v1/list-tasks`**: Xem danh sách toàn bộ các AI Task đang hoặc đã chạy trên hệ thống.
*   **`GET /api/v1/task/{task_id}`**: Lấy thông tin chi tiết đầy đủ của một Task bao gồm các bước Agent đã thực thi (steps), kết quả và lỗi (nếu có).
*   **`GET /api/v1/task/{task_id}/status`**: Dùng để polling trạng thái của Task (`running`, `finished`, `failed`, `paused`...).
*   **`PUT /api/v1/stop-task/{task_id}`**: Dừng khẩn cấp một Task đang chạy.
*   **`PUT /api/v1/pause-task/{task_id}`**: Tạm dừng (Pause) quá trình xử lý của Agent đối với một Task.
*   **`PUT /api/v1/resume-task/{task_id}`**: Tiếp tục (Resume) một Task đang bị tạm dừng.

#### 3. System Utility APIs
*   **`GET /live/{task_id}`**: Trả về một trang giao diện HTML trực quan. Bạn có thể mở trên trình duyệt web để theo dõi tiến trình (Log steps và Control panel) của một AI Task.
*   **`GET /api/v1/ping`**: Health check. Dùng để kiểm tra xem server có đang hoạt động bình thường không.
*   **`GET /api/v1/providers`**: Liệt kê các nhà cung cấp AI LLM hiện tại đã được thiết lập đủ Key trong file `.env` (Ví dụ: OpenAI là `active`, Azure là `inactive`).
*   **`GET /api/v1/browser-config`**: Liệt kê thông tin cấu hình môi trường trình duyệt hiện tại (headful/headless, đường dẫn thư mục user_data_dir...).

### Douyin Scraper Flows

The system provides three distinct processing flows for Douyin scraping:
- **Luồng 1:** `POST /api/v1/douyin/channel-videos` (Lấy danh sách xong dừng) -> Loop từng URL gọi `POST /api/v1/douyin/video-detail`.
- **Luồng 2:** `POST /api/v1/douyin/channel-videos` (Lấy danh sách) -> Chờ và lặp gọi `GET /api/v1/douyin/stream-results/{task_id}` để hệ thống ngầm xử lý tiếp.
- **Luồng 3:** `POST /api/v1/scrape-douyin-channel` (Luồng cào đồng bộ trọn gói, gộp cả lấy danh sách và chi tiết).

## Usage Examples

### Starting a Task

```bash
curl -X POST http://localhost:8000/api/v1/run-task \
  -H "Content-Type: application/json" \
  -d '{"task": "Go to google.com and search for n8n automation", "ai_provider": "openai"}'
```

### Checking Task Status

```bash
curl -X GET http://localhost:8000/api/v1/task/{task_id}/status
```

### Stopping a Task

```bash
curl -X PUT http://localhost:8000/api/v1/stop-task/{task_id}
```

## Configuration Options

You can configure the service by editing the `.env` file.  Available options are grouped below:

### API Configuration

- `PORT`: The port the service will run on (default: 8000).

### LLM Provider Configuration

#### OpenAI

- `OPENAI_API_KEY`: Your OpenAI API key.
- `OPENAI_MODEL_ID`: The model to use (e.g., `gpt-4o`).
- `OPENAI_BASE_URL`: Optional custom endpoint for OpenAI compatible APIs.

#### Anthropic

- `ANTHROPIC_API_KEY`: Your Anthropic API key.
- `ANTHROPIC_MODEL_ID`: The model to use (e.g., `claude-3-opus-20240229`).

#### MistralAI

- `MISTRAL_API_KEY`: Your MistralAI API key.
- `MISTRAL_MODEL_ID`: The model to use (e.g., `mistral-large-latest`).

#### Google AI

- `GOOGLE_API_KEY`: Your Google AI API key.
- `GOOGLE_MODEL_ID`: The model to use (e.g., `gemini-1.5-pro`).

#### Ollama

- `OLLAMA_API_BASE`: The base URL for your Ollama instance.
- `OLLAMA_MODEL_ID`: The model to use (e.g., `llama3`).

#### Azure OpenAI

- `AZURE_API_KEY`: Your Azure OpenAI API key.
- `AZURE_ENDPOINT`: Your Azure OpenAI endpoint URL.
- `AZURE_DEPLOYMENT_NAME`: Your deployment name.
- `AZURE_API_VERSION`: API version to use.

### Optional Configuration

- `LOG_LEVEL`: Logging level (default: `INFO`).
- `BROWSER_USE_HEADFUL`: Set to `"true"` to run the browser in headful mode (default: `false`, runs in headless mode).

## Troubleshooting

- **ImportError with browser-use**: Make sure you have installed the browser-use package and its dependencies correctly.
- **API Key Issues**: Verify that your API keys are correctly set in the `.env` file.
- **Port Conflicts**: If port 8000 is already in use, set a different port in the `.env` file.

## License

This project is licensed under the MIT License - see the LICENSE file for details.

## Acknowledgements

- [Browser Use](https://github.com/browser-use/browser-use) - The underlying browser automation library
- [FastAPI](https://fastapi.tiangolo.com/) - The web framework used
- [n8n](https://n8n.io/) - The workflow automation platform this bridge is designed for # browser-n8n-local
