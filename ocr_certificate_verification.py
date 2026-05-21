"""
MicroVault - OCR Certificate Verification System
Tesseract AI Integration for Credential Authentication

This module provides OCR capabilities to extract and verify certificate data
using Google's Tesseract OCR engine with advanced validation.
"""

import cv2
import pytesseract
import numpy as np
from PIL import Image
import os
from datetime import datetime
import re
from typing import Dict, Tuple, List
import logging

logger = logging.getLogger(__name__)


class CertificateOCRProcessor:
    """
    Advanced OCR processor for certificate verification
    Extracts text from certificate images using Tesseract
    """

    def __init__(self):
        """Initialize OCR processor with Tesseract configuration"""
        self.tesseract_config = r'--oem 3 --psm 6'
        self.min_confidence = 0.6
        self.supported_formats = ['.jpg', '.jpeg', '.png', '.pdf', '.bmp', '.tiff']

    def preprocess_image(self, image_path: str) -> np.ndarray:
        """
        Preprocess certificate image for better OCR accuracy
        
        Args:
            image_path: Path to certificate image
            
        Returns:
            Preprocessed image as numpy array
        """
        try:
            # Read image
            image = cv2.imread(image_path)
            if image is None:
                raise ValueError(f"Failed to load image: {image_path}")

            # Convert to grayscale
            gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)

            # Apply Gaussian blur to reduce noise
            blurred = cv2.GaussianBlur(gray, (5, 5), 0)

            # Apply Otsu's thresholding
            _, thresh = cv2.threshold(blurred, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

            # Apply morphological operations
            kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (2, 2))
            morph = cv2.morphologyEx(thresh, cv2.MORPH_CLOSE, kernel)

            # Deskew image if needed
            deskewed = self._deskew_image(morph)

            logger.info(f"Image preprocessing completed for: {image_path}")
            return deskewed

        except Exception as e:
            logger.error(f"Image preprocessing failed: {str(e)}")
            raise

    def _deskew_image(self, image: np.ndarray) -> np.ndarray:
        """
        Deskew certificate image for better text detection
        
        Args:
            image: Input image
            
        Returns:
            Deskewed image
        """
        try:
            coords = np.column_stack(np.where(image > 0))
            angle = cv2.minAreaRect(cv2.convexHull(coords))[2]

            if angle < -45:
                angle = 90 + angle

            if angle > 0:
                (h, w) = image.shape[:2]
                center = (w // 2, h // 2)
                M = cv2.getRotationMatrix2D(center, angle, 1.0)
                rotated = cv2.warpAffine(image, M, (w, h), 
                                        borderMode=cv2.BORDER_REFLECT)
                return rotated

            return image
        except Exception as e:
            logger.warning(f"Image deskewing failed: {str(e)}. Using original image.")
            return image

    def extract_text(self, image_path: str) -> Dict:
        """
        Extract all text from certificate image using Tesseract OCR
        
        Args:
            image_path: Path to certificate image
            
        Returns:
            Dictionary with extracted text data and confidence scores
        """
        try:
            # Preprocess image
            processed_image = self.preprocess_image(image_path)

            # Convert back to PIL Image for pytesseract
            pil_image = Image.fromarray(processed_image)

            # Extract text with Tesseract
            extracted_text = pytesseract.image_to_string(
                pil_image,
                config=self.tesseract_config
            )

            # Get detailed output with confidence scores
            detailed_data = pytesseract.image_to_data(
                pil_image,
                config=self.tesseract_config,
                output_type=pytesseract.Output.DICT
            )

            # Calculate overall confidence
            confidences = [int(conf) for conf in detailed_data['confidence'] if int(conf) > 0]
            avg_confidence = np.mean(confidences) / 100 if confidences else 0

            result = {
                'raw_text': extracted_text,
                'confidence': avg_confidence,
                'detailed_data': detailed_data,
                'text_blocks': self._parse_text_blocks(detailed_data),
                'success': True,
                'timestamp': datetime.now().isoformat()
            }

            logger.info(f"OCR extraction completed. Confidence: {avg_confidence:.2%}")
            return result

        except Exception as e:
            logger.error(f"OCR extraction failed: {str(e)}")
            return {
                'success': False,
                'error': str(e),
                'timestamp': datetime.now().isoformat()
            }

    def _parse_text_blocks(self, data: Dict) -> List[Dict]:
        """
        Parse OCR output into logical text blocks
        
        Args:
            data: Tesseract output data
            
        Returns:
            List of text blocks with bounding boxes
        """
        blocks = []
        n_boxes = len(data['level'])

        for i in range(n_boxes):
            if data['text'][i].strip():
                block = {
                    'text': data['text'][i],
                    'confidence': int(data['confidence'][i]),
                    'x': int(data['left'][i]),
                    'y': int(data['top'][i]),
                    'width': int(data['width'][i]),
                    'height': int(data['height'][i]),
                    'level': int(data['level'][i])
                }
                blocks.append(block)

        return blocks


class CertificateValidator:
    """
    Validates extracted certificate data against known patterns and rules
    """

    def __init__(self):
        """Initialize certificate validator with regex patterns"""
        self.patterns = {
            'date': r'\d{1,2}[/-]\d{1,2}[/-]\d{2,4}',
            'name': r'^[A-Za-z\s\'-]{3,50}$',
            'certificate_number': r'[A-Z0-9]{5,20}',
            'institution': r'[A-Za-z0-9\s&\-,\.]{5,100}',
            'email': r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}',
            'phone': r'\+?[\d\s\-\(\)]{10,20}'
        }

    def validate_certificate_fields(self, ocr_data: Dict) -> Dict:
        """
        Validate extracted certificate fields
        
        Args:
            ocr_data: Extracted OCR data
            
        Returns:
            Validation results with scores
        """
        if not ocr_data.get('success'):
            return {'valid': False, 'error': ocr_data.get('error')}

        raw_text = ocr_data['raw_text'].lower()
        validation_results = {
            'valid': False,
            'confidence_score': ocr_data['confidence'],
            'fields_detected': {},
            'issues': [],
            'warnings': []
        }

        # Check for common certificate keywords
        certificate_keywords = [
            'certificate', 'diploma', 'award', 'credential',
            'certification', 'recognized', 'presented', 'awarded to'
        ]

        keyword_found = any(keyword in raw_text for keyword in certificate_keywords)

        if not keyword_found:
            validation_results['warnings'].append('No certificate keywords detected')

        # Validate extracted fields
        fields = self._extract_fields(raw_text)
        validation_results['fields_detected'] = fields

        # Check for essential fields
        essential_fields = ['date', 'institution']
        missing_fields = [f for f in essential_fields if not fields.get(f)]

        if missing_fields:
            validation_results['issues'].append(f"Missing fields: {', '.join(missing_fields)}")
        else:
            validation_results['valid'] = True

        # Check confidence threshold
        if ocr_data['confidence'] < 0.6:
            validation_results['warnings'].append(
                f"Low OCR confidence: {ocr_data['confidence']:.2%}"
            )

        return validation_results

    def _extract_fields(self, text: str) -> Dict:
        """
        Extract and validate specific fields from text
        
        Args:
            text: OCR extracted text
            
        Returns:
            Dictionary of validated fields
        """
        fields = {}

        # Extract dates
        dates = re.findall(self.patterns['date'], text)
        fields['date'] = dates[0] if dates else None

        # Extract certificate number
        cert_numbers = re.findall(self.patterns['certificate_number'], text)
        fields['certificate_number'] = cert_numbers[0] if cert_numbers else None

        # Extract emails
        emails = re.findall(self.patterns['email'], text)
        fields['email'] = emails[0] if emails else None

        # Extract phone numbers
        phones = re.findall(self.patterns['phone'], text)
        fields['phone'] = phones[0] if phones else None

        # Try to identify institution (look for common patterns)
        institution = self._extract_institution(text)
        fields['institution'] = institution

        return fields

    def _extract_institution(self, text: str) -> str:
        """
        Extract institution/organization name from text
        
        Args:
            text: OCR extracted text
            
        Returns:
            Institution name or None
        """
        lines = text.split('\n')
        
        # Look for lines with 'university', 'college', 'school', 'institute'
        keywords = ['university', 'college', 'school', 'institute', 'academy', 'board']
        
        for line in lines:
            if any(keyword in line.lower() for keyword in keywords):
                return line.strip()
        
        return None

    def verify_authenticity(self, ocr_data: Dict, stored_credential: Dict) -> Dict:
        """
        Verify certificate authenticity against stored credential data
        
        Args:
            ocr_data: Extracted OCR data
            stored_credential: Stored credential data for comparison
            
        Returns:
            Authenticity verification results
        """
        verification = {
            'authentic': False,
            'match_score': 0,
            'matches': {},
            'mismatches': []
        }

        if not ocr_data.get('success'):
            verification['mismatches'].append('OCR extraction failed')
            return verification

        fields = ocr_data.get('fields_detected', {})

        # Compare fields with stored data
        comparisons = {
            'certificate_number': stored_credential.get('certificate_id'),
            'date': stored_credential.get('issue_date'),
            'institution': stored_credential.get('issuer')
        }

        matches = 0
        total_checks = 0

        for field_name, stored_value in comparisons.items():
            if stored_value:
                ocr_value = fields.get(field_name)
                total_checks += 1

                if ocr_value and self._similarity_score(str(ocr_value), str(stored_value)) > 0.7:
                    verification['matches'][field_name] = True
                    matches += 1
                else:
                    verification['mismatches'].append(
                        f"Field mismatch: {field_name}"
                    )

        if total_checks > 0:
            verification['match_score'] = matches / total_checks

        verification['authentic'] = (
            verification['match_score'] > 0.7 and
            ocr_data.get('confidence', 0) > 0.6
        )

        return verification

    def _similarity_score(self, str1: str, str2: str) -> float:
        """
        Calculate similarity score between two strings
        
        Args:
            str1: First string
            str2: Second string
            
        Returns:
            Similarity score between 0 and 1
        """
        from difflib import SequenceMatcher
        return SequenceMatcher(None, str1.lower(), str2.lower()).ratio()


class CertificateOCRService:
    """
    Main service for certificate OCR verification
    Integrates processor and validator
    """

    def __init__(self):
        """Initialize OCR service"""
        self.processor = CertificateOCRProcessor()
        self.validator = CertificateValidator()

    def process_certificate(self, image_path: str) -> Dict:
        """
        Complete certificate OCR processing
        
        Args:
            image_path: Path to certificate image
            
        Returns:
            Complete processing results
        """
        result = {
            'image_path': image_path,
            'processing_status': 'started',
            'steps': {}
        }

        try:
            # Step 1: Extract text
            logger.info("Step 1: Extracting text from certificate...")
            ocr_result = self.processor.extract_text(image_path)
            result['steps']['ocr_extraction'] = ocr_result

            if not ocr_result['success']:
                result['processing_status'] = 'failed'
                return result

            # Step 2: Validate fields
            logger.info("Step 2: Validating extracted fields...")
            validation = self.validator.validate_certificate_fields(ocr_result)
            result['steps']['field_validation'] = validation

            # Step 3: Extract structured data
            logger.info("Step 3: Extracting structured data...")
            structured_data = {
                'certificate_number': ocr_result['fields_detected'].get('certificate_number'),
                'issue_date': ocr_result['fields_detected'].get('date'),
                'issuer': ocr_result['fields_detected'].get('institution'),
                'holder_email': ocr_result['fields_detected'].get('email'),
                'ocr_confidence': ocr_result['confidence']
            }
            result['steps']['structured_data'] = structured_data

            result['processing_status'] = 'completed'
            result['summary'] = {
                'certificate_valid': validation.get('valid'),
                'ocr_confidence': ocr_result['confidence'],
                'fields_detected': len([f for f in ocr_result['fields_detected'].values() if f]),
                'timestamp': datetime.now().isoformat()
            }

            logger.info(f"Certificate processing completed successfully")

        except Exception as e:
            logger.error(f"Certificate processing failed: {str(e)}")
            result['processing_status'] = 'failed'
            result['error'] = str(e)

        return result

    def verify_against_stored(self, image_path: str, stored_credential: Dict) -> Dict:
        """
        Complete verification including authenticity check
        
        Args:
            image_path: Path to certificate image
            stored_credential: Stored credential data
            
        Returns:
            Complete verification results
        """
        # Process certificate
        processing_result = self.process_certificate(image_path)

        if processing_result['processing_status'] != 'completed':
            return {
                'verification_status': 'failed',
                'error': 'Certificate processing failed',
                'processing_result': processing_result
            }

        # Verify authenticity
        ocr_data = processing_result['steps']['ocr_extraction']
        authenticity = self.validator.verify_authenticity(ocr_data, stored_credential)

        return {
            'verification_status': 'completed',
            'processing_result': processing_result,
            'authenticity': authenticity,
            'final_verdict': 'VERIFIED' if authenticity['authentic'] else 'UNVERIFIED',
            'timestamp': datetime.now().isoformat()
        }


# Flask integration helper
def setup_ocr_service():
    """
    Initialize and return OCR service for Flask app
    """
    return CertificateOCRService()


# Database model for OCR results
class CertificateOCRResult:
    """
    Stores OCR verification results in database
    """
    
    def __init__(self, credential_id, image_path, ocr_data, validation_data, 
                 authenticity_data, verification_status):
        self.credential_id = credential_id
        self.image_path = image_path
        self.ocr_data = ocr_data  # JSON
        self.validation_data = validation_data  # JSON
        self.authenticity_data = authenticity_data  # JSON
        self.verification_status = verification_status  # 'VERIFIED', 'UNVERIFIED', 'FAILED'
        self.ocr_confidence = ocr_data.get('confidence', 0)
        self.verified_at = datetime.now()
        self.is_authentic = authenticity_data.get('authentic', False)
