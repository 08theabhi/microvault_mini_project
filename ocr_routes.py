"""
MicroVault - OCR Certificate Verification Routes
Flask endpoints for certificate upload and OCR verification
"""

from flask import Blueprint, request, jsonify, render_template, session, redirect
from werkzeug.utils import secure_filename
import os
from datetime import datetime
from ocr_certificate_verification import CertificateOCRService
import logging

logger = logging.getLogger(__name__)

# Initialize blueprint
ocr_blueprint = Blueprint('ocr', __name__, url_prefix='/ocr')

# Initialize OCR service
ocr_service = CertificateOCRService()

# Configuration
UPLOAD_FOLDER = 'static/uploads/certificates'
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'bmp', 'tiff', 'gif'}
MAX_FILE_SIZE = 10 * 1024 * 1024  # 10MB


def allowed_file(filename):
    """Check if file extension is allowed"""
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def save_uploaded_file(file):
    """Save uploaded file securely"""
    if file and allowed_file(file.filename):
        filename = secure_filename(file.filename)
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S_')
        filename = timestamp + filename
        filepath = os.path.join(UPLOAD_FOLDER, filename)
        os.makedirs(UPLOAD_FOLDER, exist_ok=True)
        file.save(filepath)
        return filepath
    return None


@ocr_blueprint.route('/process', methods=['POST'])
def process_certificate():
    """
    Process certificate image with OCR
    POST /ocr/process
    
    Request:
        - multipart/form-data
        - file: Certificate image file
        - credential_id: Associated credential ID (optional)
    
    Response:
        JSON with OCR results and validation
    """
    try:
        # Check if file uploaded
        if 'file' not in request.files:
            return jsonify({
                'success': False,
                'error': 'No file provided',
                'code': 'NO_FILE'
            }), 400

        file = request.files['file']
        if file.filename == '':
            return jsonify({
                'success': False,
                'error': 'No file selected',
                'code': 'NO_FILE_SELECTED'
            }), 400

        # Validate file size
        file.seek(0, os.SEEK_END)
        file_size = file.tell()
        file.seek(0)

        if file_size > MAX_FILE_SIZE:
            return jsonify({
                'success': False,
                'error': f'File too large. Maximum: 10MB',
                'code': 'FILE_TOO_LARGE'
            }), 413

        # Save file
        filepath = save_uploaded_file(file)
        if not filepath:
            return jsonify({
                'success': False,
                'error': 'Invalid file format',
                'code': 'INVALID_FORMAT'
            }), 400

        # Process certificate
        logger.info(f"Processing certificate: {filepath}")
        ocr_result = ocr_service.process_certificate(filepath)

        # Add file path to result
        ocr_result['file_path'] = filepath
        ocr_result['success'] = ocr_result['processing_status'] == 'completed'

        # Log processing result
        if ocr_result['success']:
            logger.info(f"Certificate processed successfully: {filepath}")
            return jsonify(ocr_result), 200
        else:
            logger.error(f"Certificate processing failed: {ocr_result.get('error')}")
            return jsonify(ocr_result), 400

    except Exception as e:
        logger.error(f"Certificate processing exception: {str(e)}")
        return jsonify({
            'success': False,
            'error': f'Server error: {str(e)}',
            'code': 'SERVER_ERROR'
        }), 500


@ocr_blueprint.route('/verify', methods=['POST'])
def verify_certificate():
    """
    Complete certificate verification with authenticity check
    POST /ocr/verify
    
    Request:
        - multipart/form-data
        - file: Certificate image file
        - stored_credential: JSON string of stored credential data
    
    Response:
        JSON with full verification results and authenticity status
    """
    try:
        if 'file' not in request.files:
            return jsonify({
                'success': False,
                'error': 'No file provided'
            }), 400

        file = request.files['file']
        if file.filename == '':
            return jsonify({
                'success': False,
                'error': 'No file selected'
            }), 400

        # Get stored credential data
        import json
        stored_credential = {}
        if 'stored_credential' in request.form:
            try:
                stored_credential = json.loads(request.form.get('stored_credential', '{}'))
            except json.JSONDecodeError:
                logger.warning("Invalid stored credential JSON")

        # Save file
        filepath = save_uploaded_file(file)
        if not filepath:
            return jsonify({
                'success': False,
                'error': 'Invalid file format'
            }), 400

        # Verify certificate
        logger.info(f"Verifying certificate: {filepath}")
        verification_result = ocr_service.verify_against_stored(filepath, stored_credential)

        verification_result['success'] = (
            verification_result['verification_status'] == 'completed'
        )
        verification_result['file_path'] = filepath

        if verification_result['success']:
            logger.info(f"Certificate verified: {verification_result['final_verdict']}")
            return jsonify(verification_result), 200
        else:
            logger.error(f"Certificate verification failed")
            return jsonify(verification_result), 400

    except Exception as e:
        logger.error(f"Certificate verification exception: {str(e)}")
        return jsonify({
            'success': False,
            'error': f'Server error: {str(e)}'
        }), 500


@ocr_blueprint.route('/batch-process', methods=['POST'])
def batch_process_certificates():
    """
    Process multiple certificates
    POST /ocr/batch-process
    
    Request:
        - multipart/form-data
        - files[]: Multiple certificate image files
    
    Response:
        JSON with results for all certificates
    """
    try:
        if 'files[]' not in request.files:
            return jsonify({
                'success': False,
                'error': 'No files provided',
                'processed': 0,
                'failed': 0
            }), 400

        files = request.files.getlist('files[]')
        if not files or len(files) == 0:
            return jsonify({
                'success': False,
                'error': 'No files selected',
                'processed': 0,
                'failed': 0
            }), 400

        results = {
            'success': True,
            'total': len(files),
            'processed': 0,
            'failed': 0,
            'results': []
        }

        logger.info(f"Batch processing {len(files)} certificates")

        for file in files:
            try:
                if not file or file.filename == '':
                    results['failed'] += 1
                    continue

                if not allowed_file(file.filename):
                    results['failed'] += 1
                    continue

                filepath = save_uploaded_file(file)
                if not filepath:
                    results['failed'] += 1
                    continue

                # Process certificate
                ocr_result = ocr_service.process_certificate(filepath)
                ocr_result['file_name'] = file.filename
                ocr_result['file_path'] = filepath

                results['results'].append(ocr_result)

                if ocr_result['processing_status'] == 'completed':
                    results['processed'] += 1
                else:
                    results['failed'] += 1

            except Exception as e:
                logger.error(f"Batch processing failed for {file.filename}: {str(e)}")
                results['failed'] += 1

        logger.info(f"Batch processing completed: {results['processed']} processed, "
                   f"{results['failed']} failed")

        return jsonify(results), 200

    except Exception as e:
        logger.error(f"Batch processing exception: {str(e)}")
        return jsonify({
            'success': False,
            'error': f'Server error: {str(e)}',
            'processed': 0,
            'failed': 0
        }), 500


@ocr_blueprint.route('/upload-page', methods=['GET'])
def ocr_upload_page():
    """
    Display OCR certificate upload page
    GET /ocr/upload-page
    """
    if not session.get('user_id'):
        return redirect('/login')

    return render_template('ocr_upload.html',
                          user_role=session.get('role'),
                          user_name=session.get('name'))


@ocr_blueprint.route('/results/<result_id>', methods=['GET'])
def get_ocr_results(result_id):
    """
    Get OCR results for a specific credential
    GET /ocr/results/<result_id>
    """
    try:
        if not session.get('user_id'):
            return jsonify({'error': 'Not authenticated'}), 401

        # Fetch from database (pseudo-code)
        # ocr_result = OCRResult.query.get(result_id)
        # if not ocr_result:
        #     return jsonify({'error': 'Result not found'}), 404

        return jsonify({
            'success': True,
            'result_id': result_id,
            # Include OCR results from database
        }), 200

    except Exception as e:
        logger.error(f"Failed to get OCR results: {str(e)}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@ocr_blueprint.route('/comparison', methods=['POST'])
def compare_ocr_results():
    """
    Compare OCR results from multiple uploads of same certificate
    POST /ocr/comparison
    
    Request:
        - multipart/form-data
        - files[]: Multiple images of same certificate
    
    Response:
        JSON with comparison analysis and consistency report
    """
    try:
        files = request.files.getlist('files[]')
        if len(files) < 2:
            return jsonify({
                'success': False,
                'error': 'Need at least 2 images to compare'
            }), 400

        results = {
            'success': True,
            'total_images': len(files),
            'individual_results': [],
            'consistency_analysis': {},
            'overall_confidence': 0
        }

        extracted_fields_list = []

        # Process each image
        for idx, file in enumerate(files):
            try:
                filepath = save_uploaded_file(file)
                if not filepath:
                    continue

                ocr_result = ocr_service.process_certificate(filepath)
                ocr_result['image_index'] = idx + 1
                results['individual_results'].append(ocr_result)

                if ocr_result['success']:
                    extracted_fields_list.append(ocr_result.get('fields_detected', {}))

            except Exception as e:
                logger.error(f"Comparison processing failed: {str(e)}")

        # Analyze consistency
        if extracted_fields_list:
            consistency = _analyze_field_consistency(extracted_fields_list)
            results['consistency_analysis'] = consistency
            results['overall_confidence'] = (
                sum(r.get('confidence', 0) for r in results['individual_results'])
                / len(results['individual_results'])
            )

        logger.info(f"Comparison analysis completed for {len(files)} images")
        return jsonify(results), 200

    except Exception as e:
        logger.error(f"Comparison analysis exception: {str(e)}")
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


def _analyze_field_consistency(fields_list):
    """
    Analyze consistency of extracted fields across multiple images
    
    Args:
        fields_list: List of field dictionaries from multiple OCR results
        
    Returns:
        Consistency analysis report
    """
    from difflib import SequenceMatcher

    analysis = {
        'field_consistency': {},
        'consistent': True,
        'confidence_score': 0
    }

    if not fields_list:
        return analysis

    # Get all unique field names
    all_fields = set()
    for fields in fields_list:
        all_fields.update(fields.keys())

    consistency_scores = []

    # Check consistency for each field
    for field_name in all_fields:
        field_values = [
            str(fields.get(field_name, ''))
            for fields in fields_list
            if fields.get(field_name)
        ]

        if len(field_values) >= 2:
            # Calculate similarity between values
            similarity = SequenceMatcher(
                None,
                field_values[0].lower(),
                field_values[1].lower()
            ).ratio()

            analysis['field_consistency'][field_name] = {
                'values': field_values,
                'consistency_score': similarity,
                'consistent': similarity > 0.8
            }

            if similarity <= 0.8:
                analysis['consistent'] = False

            consistency_scores.append(similarity)

    if consistency_scores:
        analysis['confidence_score'] = sum(consistency_scores) / len(consistency_scores)

    return analysis


# Register blueprint with Flask app
def register_ocr_routes(app):
    """
    Register OCR routes with Flask application
    
    Usage in main app.py:
        from routes.ocr_routes import register_ocr_routes
        register_ocr_routes(app)
    """
    app.register_blueprint(ocr_blueprint)
    logger.info("OCR routes registered successfully")
