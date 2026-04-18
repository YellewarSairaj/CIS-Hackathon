import os
import logging
import hashlib
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, send_from_directory
from azure.storage.blob import BlobServiceClient
from azure.data.tables import TableServiceClient
from azure.core.exceptions import AzureError, ResourceNotFoundError
from werkzeug.utils import secure_filename
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

app = Flask(__name__)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Azure Storage Configuration
AZURE_STORAGE_CONNECTION_STRING = os.getenv("AZURE_STORAGE_CONNECTION_STRING")
BLOB_CONTAINER_NAME = "files"
TABLE_NAME = "filemetadata"
PARTITION_KEY = "files"

# Allowed file extensions
ALLOWED_EXTENSIONS = {"txt", "pdf", "png", "jpg", "jpeg", "gif", "docx", "xlsx"}

# Validate connection string on startup
if not AZURE_STORAGE_CONNECTION_STRING:
    logger.error("AZURE_STORAGE_CONNECTION_STRING environment variable not set")
    raise ValueError(
        "AZURE_STORAGE_CONNECTION_STRING environment variable not set. "
        "Please create a .env file with your connection string or set the environment variable."
    )

logger.info("Azure Storage connection string loaded successfully")

def calculate_file_hash(file_content):
    """Calculate SHA256 hash of file content"""
    return hashlib.sha256(file_content).hexdigest()

def get_blob_service_client():
    """Initialize and return BlobServiceClient"""
    return BlobServiceClient.from_connection_string(AZURE_STORAGE_CONNECTION_STRING)

def get_table_service_client():
    """Initialize and return TableServiceClient"""
    return TableServiceClient.from_connection_string(AZURE_STORAGE_CONNECTION_STRING)

def get_container_client():
    """Get container client for blob storage, create if doesn't exist"""
    try:
        blob_client = get_blob_service_client()
        container_client = blob_client.get_container_client(BLOB_CONTAINER_NAME)
        
        # Check if container exists, create if not
        try:
            container_client.get_container_properties()
            logger.debug(f"Container '{BLOB_CONTAINER_NAME}' exists")
        except ResourceNotFoundError:
            logger.info(f"Container '{BLOB_CONTAINER_NAME}' not found, creating...")
            container_client = blob_client.create_container(name=BLOB_CONTAINER_NAME)
            logger.info(f"Container '{BLOB_CONTAINER_NAME}' created successfully")
        
        return container_client
    except AzureError as e:
        logger.error(f"Failed to get/create container: {str(e)}")
        raise

def get_table_client():
    """Get table client for metadata storage, create if doesn't exist"""
    try:
        table_service = get_table_service_client()
        table_client = table_service.get_table_client(TABLE_NAME)
        
        # Try to create table if it doesn't exist
        try:
            table_service.create_table(table_name=TABLE_NAME)
            logger.info(f"Table '{TABLE_NAME}' created successfully")
        except Exception as e:
            # Table already exists, that's fine
            if "TableAlreadyExists" not in str(e):
                logger.debug(f"Table '{TABLE_NAME}' already exists or other error: {str(e)}")
            logger.debug(f"Table '{TABLE_NAME}' already exists")
        
        return table_client
    except AzureError as e:
        logger.error(f"Failed to get/create table: {str(e)}")
        raise

def allowed_file(filename):
    """Check if file extension is allowed"""
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS

@app.route("/upload", methods=["POST"])
def upload_file():
    """
    Upload a file to Azure Blob Storage with version control.
    Maintains version history in Table Storage.
    """
    try:
        logger.info("Processing file upload request with version control")
        
        if "file" not in request.files:
            logger.warning("Upload request missing 'file' field")
            return jsonify({"error": "No file provided"}), 400
        
        file = request.files["file"]
        
        if file.filename == "":
            logger.warning("Upload request received with empty filename")
            return jsonify({"error": "No file selected"}), 400
        
        filename = secure_filename(file.filename)
        
        if not filename:
            logger.warning(f"Invalid filename after sanitization: {file.filename}")
            return jsonify({"error": "Invalid filename"}), 400
        
        if not allowed_file(filename):
            allowed = ", ".join(sorted(ALLOWED_EXTENSIONS))
            logger.warning(f"File type not allowed: {filename}")
            return jsonify({
                "error": f"File type not allowed. Allowed types: {allowed}"
            }), 400
        
        # Read file content
        file.seek(0)
        file_content = file.read()
        file_hash = calculate_file_hash(file_content)
        
        # Upload to Blob Storage
        try:
            logger.info(f"Uploading file '{filename}' to Blob Storage")
            container_client = get_container_client()
            
            # Create versioned blob name
            timestamp = datetime.utcnow().isoformat()
            blob_name = f"{filename}_{timestamp}"
            
            container_client.upload_blob(blob_name, file_content, overwrite=True)
            logger.info(f"File '{filename}' uploaded as '{blob_name}'")
        except AzureError as e:
            logger.error(f"Failed to upload to Blob Storage: {str(e)}")
            return jsonify({"error": f"Failed to upload: {str(e)}"}), 500
        
        # Store metadata and version info in Table Storage
        try:
            logger.info(f"Storing version metadata for '{filename}'")
            table_client = get_table_client()
            
            # Create short row key using hash of timestamp to avoid length limits
            import uuid
            version_id = str(uuid.uuid4())[:8]
            
            # Store version record
            version_entity = {
                "PartitionKey": filename,  # Use filename as partition key for easier querying
                "RowKey": version_id,      # Short unique ID
                "fileName": filename,
                "blobName": blob_name,
                "fileHash": file_hash,
                "fileSize": len(file_content),
                "uploadedAt": timestamp
            }
            
            table_client.upsert_entity(version_entity)
            
            # Update or create file metadata entry with different partition key
            metadata_entity = {
                "PartitionKey": "metadata",
                "RowKey": filename,
                "fileName": filename,
                "currentVersion": timestamp,
                "totalVersions": 1,
                "totalSize": len(file_content),
                "lastUpdated": timestamp,
                "fileHash": file_hash
            }
            
            table_client.upsert_entity(metadata_entity)
            logger.info(f"Version metadata stored successfully")
            
        except AzureError as e:
            logger.error(f"Failed to store metadata: {str(e)}")
            return jsonify({"error": f"Failed to store metadata: {str(e)}"}), 500
        
        logger.info(f"File upload completed: {filename}")
        return jsonify({
            "status": "success",
            "message": f"File {filename} uploaded successfully (v1)",
            "fileName": filename,
            "fileHash": file_hash,
            "fileSize": len(file_content)
        }), 201
    
    except Exception as e:
        logger.exception(f"Unexpected error during file upload: {str(e)}")
        return jsonify({"error": f"Unexpected error: {str(e)}"}), 500

@app.route("/health", methods=["GET"])
def health_check():
    """Health check endpoint"""
    logger.debug("Health check request received")
    return jsonify({"status": "healthy"}), 200

@app.route("/", methods=["GET"])
def index():
    """Serve the web frontend"""
    return send_from_directory('static', 'index.html')

@app.route("/static/<path:filename>", methods=["GET"])
def serve_static(filename):
    """Serve static files"""
    return send_from_directory('static', filename)

@app.route("/files", methods=["GET"])
def get_files():
    """
    Retrieve all file metadata from Azure Table Storage.
    Returns: JSON list of uploaded files with metadata
    """
    try:
        logger.info(f"Retrieving files from Table Storage table '{TABLE_NAME}'")
        table_client = get_table_client()
        
        # Query all metadata entities (PartitionKey == "metadata")
        filter_query = f"PartitionKey eq 'metadata'"
        entities = table_client.query_entities(filter_query)
        
        files_list = []
        for entity in entities:
            file_info = {
                "fileName": entity.get("fileName", ""),
                "totalVersions": entity.get("totalVersions", 0),
                "totalSize": entity.get("totalSize", 0),
                "lastUpdated": entity.get("lastUpdated", ""),
            }
            files_list.append(file_info)
            logger.debug(f"Retrieved file metadata: {file_info}")
        
        logger.info(f"Retrieved {len(files_list)} files from Table Storage")
        return jsonify({
            "status": "success",
            "count": len(files_list),
            "files": files_list
        }), 200
    
    except AzureError as e:
        logger.error(f"Failed to retrieve files from Table Storage: {str(e)}")
        return jsonify({
            "error": f"Failed to retrieve files from Table Storage: {str(e)}"
        }), 500
    except Exception as e:
        logger.exception(f"Unexpected error retrieving files: {str(e)}")
        return jsonify({"error": f"Unexpected error: {str(e)}"}), 500

@app.route("/files/<filename>/versions", methods=["GET"])
def get_file_versions(filename):
    """Get all versions of a specific file"""
    try:
        logger.info(f"Retrieving versions for file: {filename}")
        table_client = get_table_client()
        
        # Query all versions of this file (PartitionKey == filename)
        filter_query = f"PartitionKey eq '{filename}'"
        entities = table_client.query_entities(filter_query)
        
        versions = []
        for entity in entities:
            version_info = {
                "timestamp": entity.get("uploadedAt", ""),
                "fileHash": entity.get("fileHash", ""),
                "fileSize": entity.get("fileSize", 0),
                "versionNumber": len(versions) + 1
            }
            versions.append(version_info)
        
        versions.sort(key=lambda x: x["timestamp"], reverse=True)
        
        logger.info(f"Found {len(versions)} versions for file: {filename}")
        return jsonify({
            "status": "success",
            "fileName": filename,
            "totalVersions": len(versions),
            "versions": versions
        }), 200
    
    except Exception as e:
        logger.error(f"Failed to retrieve versions: {str(e)}")
        return jsonify({"error": f"Failed to retrieve versions: {str(e)}"}), 500

@app.route("/backup-stats", methods=["GET"])
def get_backup_stats():
    """Get backup statistics and recommendations"""
    try:
        logger.info("Calculating backup statistics")
        table_client = get_table_client()
        
        # Get all version records from all partitions (excluding metadata)
        total_files = 0
        total_versions = 0
        total_size = 0
        duplicates = 0
        file_hashes = {}
        
        # Query metadata to get file count
        filter_query = f"PartitionKey eq 'metadata'"
        metadata_entities = list(table_client.query_entities(filter_query))
        total_files = len(metadata_entities)
        
        # Get all entities from the table to count versions
        all_entities = list(table_client.query_entities(""))
        
        for entity in all_entities:
            partition_key = entity.get("PartitionKey", "")
            
            # Skip metadata entries
            if partition_key == "metadata":
                continue
            
            # Count as version
            total_versions += 1
            file_size = entity.get("fileSize", 0)
            total_size += file_size
            
            # Check for duplicates using file hash
            file_hash = entity.get("fileHash", "")
            if file_hash in file_hashes:
                duplicates += 1
            else:
                file_hashes[file_hash] = entity.get("fileName", "")
        
        # Calculate backup frequency recommendation
        recommendation = "Weekly"  # Default
        if total_versions > 10:
            recommendation = "Daily"
        elif total_versions > 5:
            recommendation = "Every 3 Days"
        
        logger.info("Backup statistics calculated")
        return jsonify({
            "status": "success",
            "statistics": {
                "totalFiles": total_files,
                "totalVersions": total_versions,
                "totalBackupSize": round(total_size / 1024 / 1024, 2),  # MB
                "duplicateUploads": duplicates,
                "backupRecommendation": recommendation
            },
            "message": f"Recommended backup frequency: {recommendation}"
        }), 200
    
    except Exception as e:
        logger.error(f"Failed to calculate backup stats: {str(e)}")
        return jsonify({"error": f"Failed to calculate stats: {str(e)}"}), 500

@app.route("/files/<filename>/restore/<version_timestamp>", methods=["POST"])
def restore_file(filename, version_timestamp):
    """Restore a file to a previous version"""
    try:
        logger.info(f"Restoring file {filename} to version {version_timestamp}")
        
        table_client = get_table_client()
        
        # Get the version record (filename is the PartitionKey now)
        filter_query = f"PartitionKey eq '{filename}'"
        entities = list(table_client.query_entities(filter_query))
        
        # Find the matching version
        version_entity = None
        for entity in entities:
            if entity.get("uploadedAt") == version_timestamp:
                version_entity = entity
                break
        
        if not version_entity:
            logger.warning(f"Version not found: {filename}#{version_timestamp}")
            return jsonify({"error": "Version not found"}), 404
        
        blob_name = version_entity.get("blobName", "")
        
        # Create a new version with the restored content
        container_client = get_container_client()
        blob_client = container_client.get_blob_client(blob_name)
        
        # Download the blob
        blob_data = blob_client.download_blob()
        content = blob_data.readall()
        
        # Upload as current version
        timestamp = datetime.utcnow().isoformat()
        new_blob_name = f"{filename}_{timestamp}"
        container_client.upload_blob(new_blob_name, content, overwrite=True)
        
        # Create new version record with short RowKey
        import uuid
        version_id = str(uuid.uuid4())[:8]
        
        new_version = {
            "PartitionKey": filename,
            "RowKey": version_id,
            "fileName": filename,
            "blobName": new_blob_name,
            "fileHash": version_entity.get("fileHash", ""),
            "fileSize": len(content),
            "uploadedAt": timestamp,
            "restoredFrom": version_timestamp
        }
        
        table_client.upsert_entity(new_version)
        
        logger.info(f"File restored successfully: {filename}")
        return jsonify({
            "status": "success",
            "message": f"File {filename} restored to previous version",
            "restoredTimestamp": timestamp
        }), 200
    
    except Exception as e:
        logger.error(f"Failed to restore file: {str(e)}")
        return jsonify({"error": f"Failed to restore: {str(e)}"}), 500

@app.route("/duplicate-check", methods=["POST"])
def check_duplicate():
    """Check if uploaded file is a duplicate"""
    try:
        if "file" not in request.files:
            return jsonify({"error": "No file provided"}), 400
        
        file = request.files["file"]
        file.seek(0)
        content = file.read()
        file_hash = calculate_file_hash(content)
        
        logger.info(f"Checking for duplicates with hash: {file_hash}")
        
        table_client = get_table_client()
        
        # Search all entities for matching file hash
        all_entities = list(table_client.query_entities(""))
        matching_entities = [e for e in all_entities if e.get("fileHash") == file_hash]
        
        is_duplicate = len(matching_entities) > 0
        
        logger.info(f"Duplicate check result: {is_duplicate}")
        return jsonify({
            "status": "success",
            "isDuplicate": is_duplicate,
            "fileHash": file_hash,
            "message": "This file already exists!" if is_duplicate else "This is a new unique file"
        }), 200
    
    except Exception as e:
        logger.error(f"Failed to check duplicate: {str(e)}")
        return jsonify({"error": f"Failed to check: {str(e)}"}), 500

@app.errorhandler(404)
def not_found(error):
    """Handle 404 errors"""
    logger.warning(f"404 Not Found: {request.path}")
    return jsonify({"error": "Endpoint not found"}), 404

@app.errorhandler(500)
def internal_error(error):
    """Handle 500 errors"""
    logger.error(f"500 Internal Server Error: {str(error)}")
    return jsonify({"error": "Internal server error"}), 500

if __name__ == "__main__":
    logger.info("=" * 60)
    logger.info("Flask Azure Storage Application Starting")
    logger.info("=" * 60)
    logger.info(f"Blob container: {BLOB_CONTAINER_NAME}")
    logger.info(f"Table name: {TABLE_NAME}")
    logger.info(f"Partition key: {PARTITION_KEY}")
    logger.info(f"Allowed file types: {', '.join(sorted(ALLOWED_EXTENSIONS))}")
    logger.info("=" * 60)
    app.run(debug=True, host="0.0.0.0", port=9000)
