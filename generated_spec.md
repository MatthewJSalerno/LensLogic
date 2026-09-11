# Project Design Specification: [Project Name - e.g., LensLogic]

## 1. Project Overview
**Objective:** A web-based application designed to automate the organization of large, complex photo collections.
**Core Problem:** Users struggle with redundant files, non-standardized directory structures, and inconsistent metadata across multiple devices/exports.
**Primary Goals:**
*   **Standardization:** Automate moving files into a coherent structure (e.g., `/Year/Month/Date_Event/`).
*   **Deduplication:** Remove identical files using SHA1 hashing.
*   **Fuzzy Mapping:** Identify and group visually similar images using perceptual hashing (pHash).
*   **Metadata Integrity:** Preserve and synchronize EXIF data across related images.
*   **Data Safety:** Ensure all file operations are transactional, recoverable, and non-destructive.

## 2. Target Audience
*   **Primary:** Professional photographers and content creators managing thousands of assets.
*   **Secondary:** General users with large, unorganized mobile/camera backups.

## 3. System Architecture (Polyglot Design)
To balance heavy-duty data processing with a high-quality user experience, the application utilizes a multi-layered architecture:

### 3.1. The Frontend (User Experience)
*   **Technology:** Modern Web Framework (e.g., React, Vue, or Svelte).
*   **Function:** Provides a dashboard for file path configuration, "Dry Run" report viewing, "Merge" resolution, and real-time progress tracking via WebSockets.

### 3.2. The Web & API Layer (Node.js)
*   **Role:** Acts as the "Command Center" and communication bridge.
*   **Function:** Handles HTTP/WebSocket requests, manages user sessions, and relays real-time updates from the Python worker to the frontend.
*   **Communication:** Uses WebSockets (Socket.io) to provide real-time updates to the user during long-running tasks.

### 3.3. The Processing Engine (Python)
*   **Role:** The "Workhorse" for data heavy-lifting.
*   **Function:** Handles filesystem crawling, EXIF extraction, SHA1/pHash generation, and physical file manipulation.
*   **Key Libraries:** `Pillow`/`OpenCV` (image processing), `imagehash` (pHash generation), `hashlib` (SHA1), and `ExifTool` or `rawpy` for robust **RAW and DNG** metadata handling.

## 4. Functional Requirements
### 4.1. Input & Configuration
*   **Path Definitions:** Users must provide a **Source Path** (messy collection) and a **Destination Path** (organized output).
*   **Dry Run Mode (Default):** The system defaults to a "Dry Run." It performs the full scan, identifies duplicates, calculates hashes, and flags collisions, but does **not** move or delete any files.
*   **Merge/Action Mode:** Users must explicitly "Commit" changes after reviewing the Dry Run results to initiate the physical data migration.

### 4.2. Processing Logic
*   **Deduplication:**
    *   **Exact Match:** Files with identical SHA1 hashes are flagged as clones.
    *   **Fuzzy Match:** Files with similar pHashes are grouped together as "potentially similar."
*   **Metadata Collisions:** 
    *   When pHashes are similar but SHA1s differ, the system identifies these as "Metadata Collisions" (e.g., the same photo with different EXIF data or different compression).
    *   The system flags these for the user to "Merge" metadata from a primary source to the secondary.
*   **Unique Filename Enforcement:**
    *   To prevent accidental overwriting, the system checks if a file name already exists in the destination.
    *   If a collision occurs (e.g., two different photos named `IMG_001.jpg`), the system automatically appends a unique suffix (e.g., `IMG_001_1.jpg`).
*   **Broad Format Support:**
    *   Full support for standard formats (JPEG, PNG, TIFF) and professional formats (**RAW, DNG, CR2, NEF**).
*   **Standardized Movement:** Files are moved based on EXIF "Date Taken" timestamps.

### 4.3. Safety & Durability
*   **Transactional Move (Copy-Verify-Delete):**
    *   To ensure recovery from crashes or interruptions, the system never uses a standard `move`.
    *   **Step 1 (Copy):** The file is copied to the destination.
    *   **Step 2 (Verify):** The system verifies the SHA1 of the new copy.
    *   **Step 3 (Commit):** Only upon successful verification is the original file deleted from the source.
*   **Resume Capability:** Upon startup, the system checks the database for "Pending" or "Copying" states and resumes the process automatically.

### 4.4. Logging & Reporting
*   **Comprehensive Logging:** Every stage (Scanning, Hashing, Matching, Moving) must generate detailed logs.
*   **Real-time Feedback:** The UI provides a live progress bar and status messages (e.g., "Verifying," "Processing," "Resolving Collisions").

## 5. Technical Infrastructure
### 5.1. Deployment (Docker)
*   **Environment:** Containerized for cross-platform consistency.
*   **Volume Mapping:** The Docker config must map the host's local folders to internal container paths.
*   **Database:** **SQLite** is used to store the file index, hash values, and movement statuses.

### 5.2. Data Flow
1.  **User** provides paths via **Web UI**.
2.  **Node.js** receives request and triggers the **Python Worker**.
3.  **Python Worker** crawls files, calculates hashes, and updates the **SQLite DB**.
4.  **Python Worker** pushes progress/status to **Node.js** via an internal bridge.
5.  **Node.js** pushes updates to the **Web UI** via **WebSockets**.
6.  **User** reviews the Dry Run/Collisions and clicks "Commit."
7.  **Python Worker** executes the **Copy-Verify-Delete** protocol for all items.

## 6. Data Schema (SQLite)
| Field | Type | Description |
| :--- | :--- | :--- |
| `id` | Integer | Primary Key |
| `source_path` | Text | Original file path |
| `dest_path` | Text | Target path (includes unique suffixes) |
| `sha1_hash` | Text | Exact content hash |
| `phash` | Text | Perceptual hash |
| `collision_group` | Integer | ID linking items with similar pHashes |
| `is_master` | Boolean | Flag for the "Master" metadata in a collision |
| `status` | String | [Pending, Processing, Verified, Completed, Failed] |
| `metadata_json` | JSON | Extracted and preserved EXIF data |

## 7. Non-Functional Requirements
*   **Data Integrity:** No file is deleted from the source until it is verified at the destination.
*   **Durability:** System must be able to resume a "Commit" operation after a restart or crash.
*   **Performance:** Python worker should utilize multi-threading/processing for hash calculations.
*   **Safety:** Dry Run must be the default; no destructive actions occur without explicit user "Commit."
*   **Scalability:** System must handle 10,000+ images while maintaining a responsive UI.