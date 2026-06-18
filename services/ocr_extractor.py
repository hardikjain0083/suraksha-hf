import os
import io
import time
import logging
import concurrent.futures
from typing import List, Dict, Any, Union, Tuple
from PIL import Image
import numpy as np
import cv2
import pytesseract
from tenacity import retry, stop_after_attempt, wait_exponential

from config import settings

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────
#  Exceptions
# ─────────────────────────────────────────────────────────────

class SuRakshaException(Exception):
    """Base exception for all SuRaksha application errors"""
    pass

class OCRException(SuRakshaException):
    """Raised when OCR fails completely"""
    pass

class LowConfidenceException(SuRakshaException):
    """Raised when OCR confidence < threshold — triggers manual review flag"""
    def __init__(self, message: str, ocr_result: dict = None):
        super().__init__(message)
        self.ocr_result = ocr_result or {}


# Auto-configure local Tesseract binary path for Windows developers
if os.name == "nt":
    win_tess_path = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
    if os.path.exists(win_tess_path):
        pytesseract.pytesseract.tesseract_cmd = win_tess_path
        logger.info(f"Auto-configured Tesseract binary path on Windows: {win_tess_path}")


class OCRExtractor:
    def __init__(self, engine: str = "auto"):
        """
        engine options:
        - "tesseract"  -> local Tesseract OCR (fallback, free)
        - "cloud"      -> cloud OCR API (AWS Textract)
        - "auto"       -> try cloud first, fallback to tesseract if unavailable
        """
        self.engine = engine if engine in ["tesseract", "cloud", "auto"] else settings.ocr_engine

    def is_scanned_pdf(self, pdf_path: Union[str, bytes]) -> bool:
        """
        Heuristic detection:
        - Check if PDF has zero extractable text via PyPDF2 / PyMuPDF
        - Check image-to-page ratio > 0.8
        - Return True if likely scanned
        """
        try:
            from PyPDF2 import PdfReader
            import io

            if isinstance(pdf_path, bytes):
                reader = PdfReader(io.BytesIO(pdf_path))
            else:
                reader = PdfReader(pdf_path)

            total_pages = len(reader.pages)
            if total_pages == 0:
                return False

            pages_with_no_text = 0
            pages_with_images = 0

            for page in reader.pages:
                text = (page.extract_text() or "").strip()
                if not text or len(text) < 50:
                    pages_with_no_text += 1

                # Check for images on page
                has_image = False
                try:
                    if "/Resources" in page and "/XObject" in page["/Resources"]:
                        xObject = page["/Resources"]["/XObject"].get_object()
                        for obj in xObject:
                            if xObject[obj]["/Subtype"] == "/Image":
                                has_image = True
                                break
                except Exception:
                    pass
                if has_image:
                    pages_with_images += 1

            no_text = (pages_with_no_text == total_pages)
            image_ratio = pages_with_images / total_pages

            logger.info(f"Scanned PDF heuristics: total_pages={total_pages} no_text_pages={pages_with_no_text} image_pages={pages_with_images} image_ratio={image_ratio:.2f}")
            return no_text or (image_ratio > 0.8)

        except Exception as e:
            logger.warning(f"Error in is_scanned_pdf heuristic: {e}. Defaulting to scanned=True for safety.")
            return True

    def preprocess_image(self, image: Image.Image) -> Image.Image:
        """
        Preprocessing pipeline for better OCR accuracy:
        - Grayscale conversion
        - Noise reduction (median filter)
        - Contrast enhancement (adaptive histogram equalization CLAHE)
        - Deskew (if detectable skew angle > 2°)
        - Binarization (Otsu's method)
        """
        # Convert PIL image to OpenCV format
        open_cv_image = np.array(image)
        if len(open_cv_image.shape) == 3:
            # Color to BGR
            open_cv_image = cv2.cvtColor(open_cv_image, cv2.COLOR_RGB2BGR)
        else:
            # Grayscale already
            open_cv_image = cv2.cvtColor(open_cv_image, cv2.COLOR_GRAY2BGR)

        # 1. Grayscale conversion
        gray = cv2.cvtColor(open_cv_image, cv2.COLOR_BGR2GRAY)

        # 2. Noise reduction (median filter)
        denoised = cv2.medianBlur(gray, 3)

        # 3. Contrast enhancement (CLAHE adaptive histogram equalization)
        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
        enhanced = clahe.apply(denoised)

        # 4. Deskew (if detectable skew angle > 2°)
        # We threshold to invert text and detect skew using minAreaRect
        thresh = cv2.threshold(enhanced, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1]
        pts = np.column_stack(np.where(thresh > 0))
        if len(pts) > 0:
            rect = cv2.minAreaRect(pts)
            angle = rect[-1]
            # Adjust angle depending on orientation
            if angle < -45:
                angle = -(90 + angle)
            else:
                angle = -angle

            # Apply rotation if skew > 2 degrees and less than 45 degrees
            if abs(angle) > 2.0 and abs(angle) < 45.0:
                (h, w) = enhanced.shape[:2]
                center = (w // 2, h // 2)
                M = cv2.getRotationMatrix2D(center, angle, 1.0)
                enhanced = cv2.warpAffine(
                    enhanced, M, (w, h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_REPLICATE
                )
                logger.info(f"Deskew applied: rotated by {angle:.2f} degrees")

        # 5. Binarization (Otsu's method)
        _, binarized = cv2.threshold(enhanced, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

        return Image.fromarray(binarized)

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=2, max=10), reraise=True)
    def _cloud_ocr(self, image_bytes: bytes) -> Dict[str, Any]:
        """
        AWS Textract call wrapped with rate limit retry / exponential backoff
        """
        import boto3
        
        region = settings.aws_textract_region
        if not region:
            raise OCRException("AWS_TEXTRACT_REGION is not set. Cloud OCR requires a configured region.")

        aws_config = {
            "region_name": region
        }
        if settings.aws_access_key_id:
            aws_config["aws_access_key_id"] = settings.aws_access_key_id
        if settings.aws_secret_access_key:
            aws_config["aws_secret_access_key"] = settings.aws_secret_access_key

        client = boto3.client("textract", **aws_config)

        # Try to use analyze_document for tables, fallback to detect_document_text if tables fail
        try:
            response = client.analyze_document(
                Document={"Bytes": image_bytes},
                FeatureTypes=["TABLES"]
            )
        except Exception as e:
            logger.warning(f"analyze_document failed: {e}. Falling back to detect_document_text.")
            response = client.detect_document_text(
                Document={"Bytes": image_bytes}
            )
        
        return response

    def _parse_textract_response(self, response: Dict[str, Any], page_index: int) -> Tuple[str, float, List[Dict[str, Any]]]:
        """
        Extract text, confidence, and structured tables from AWS Textract response
        """
        blocks = response.get("Blocks", [])
        lines = [b.get("Text", "") for b in blocks if b.get("BlockType") == "LINE"]
        full_text = "\n".join(lines)

        # Confidence of WORD blocks
        word_confidences = [b.get("Confidence", 100.0) / 100.0 for b in blocks if b.get("BlockType") == "WORD"]
        avg_confidence = sum(word_confidences) / len(word_confidences) if word_confidences else 1.0

        # Table Parsing
        extracted_tables = []
        block_map = {b["Id"]: b for b in blocks}
        table_blocks = [b for b in blocks if b.get("BlockType") == "TABLE"]

        for table in table_blocks:
            cells = []
            max_row = 0
            max_col = 0
            for relationship in table.get("Relationships", []):
                if relationship["Type"] == "CHILD":
                    for child_id in relationship["Ids"]:
                        cell = block_map.get(child_id)
                        if cell and cell.get("BlockType") == "CELL":
                            cells.append(cell)
                            max_row = max(max_row, cell.get("RowIndex", 0))
                            max_col = max(max_col, cell.get("ColumnIndex", 0))

            if not cells:
                continue

            grid = [["" for _ in range(max_col)] for _ in range(max_row)]
            cell_confidences = []

            for cell in cells:
                r = cell.get("RowIndex", 1) - 1
                c = cell.get("ColumnIndex", 1) - 1
                cell_text = ""
                for cell_rel in cell.get("Relationships", []):
                    if cell_rel["Type"] == "CHILD":
                        for cell_child_id in cell_rel["Ids"]:
                            word_block = block_map.get(cell_child_id)
                            if word_block and word_block.get("BlockType") == "WORD":
                                cell_text += word_block.get("Text", "") + " "
                
                grid[r][c] = cell_text.strip()
                cell_confidences.append(cell.get("Confidence", 100.0) / 100.0)

            table_avg_conf = sum(cell_confidences) / len(cell_confidences) if cell_confidences else 1.0
            extracted_tables.append({
                "page": page_index + 1,
                "rows": max_row,
                "columns": max_col,
                "data": grid,
                "confidence": round(table_avg_conf, 3)
            })

        return full_text, avg_confidence, extracted_tables

    def _process_single_page(self, page_img: Image.Image, page_idx: int, engine_to_use: str) -> Dict[str, Any]:
        """
        Process a single page: Preprocess + OCR
        """
        start_time = time.time()
        preprocessed = self.preprocess_image(page_img)

        text = ""
        confidence = 0.0
        tables = []
        engine_actual = "none"

        # Try Cloud first if auto or cloud
        if engine_to_use in ["cloud", "auto"]:
            try:
                # Save PIL to bytes
                img_byte_arr = io.BytesIO()
                preprocessed.save(img_byte_arr, format="PNG")
                img_bytes = img_byte_arr.getvalue()

                response = self._cloud_ocr(img_bytes)
                text, confidence, tables = self._parse_textract_response(response, page_idx)
                engine_actual = "cloud"
            except Exception as e:
                logger.error(f"Cloud OCR failed on page {page_idx}: {e}")
                if engine_to_use == "auto":
                    logger.info(f"Falling back to local Tesseract for page {page_idx}")
                    engine_to_use = "tesseract"
                else:
                    return {
                        "text": "",
                        "confidence": 0.0,
                        "tables": [],
                        "engine_used": "none",
                        "error": str(e),
                        "duration_ms": int((time.time() - start_time) * 1000)
                    }

        # Local Tesseract
        if engine_to_use == "tesseract":
            try:
                text = pytesseract.image_to_string(preprocessed)
                
                # Confidence
                data = pytesseract.image_to_data(preprocessed, output_type=pytesseract.Output.DICT)
                confidences = [float(c) for c in data.get("conf", []) if c not in ("-1", -1)]
                confidence = (sum(confidences) / len(confidences) / 100.0) if confidences else 0.0
                engine_actual = "tesseract"
            except Exception as e:
                logger.error(f"Tesseract OCR failed on page {page_idx}: {e}")
                return {
                    "text": "",
                    "confidence": 0.0,
                    "tables": [],
                    "engine_used": "none",
                    "error": str(e),
                    "duration_ms": int((time.time() - start_time) * 1000)
                }

        duration_ms = int((time.time() - start_time) * 1000)
        return {
            "text": text,
            "confidence": confidence,
            "tables": tables,
            "engine_used": engine_actual,
            "duration_ms": duration_ms
        }

    def extract_text(self, pdf_path: Union[str, bytes]) -> Dict[str, Any]:
        """
        Extracts text from PDF file or bytes with full OCR pipeline support.
        """
        start_time = time.time()
        
        # Load PDF bytes
        if isinstance(pdf_path, bytes):
            pdf_bytes = pdf_path
        else:
            with open(pdf_path, "rb") as f:
                pdf_bytes = f.read()

        # Heuristic scanned detection
        is_scanned = self.is_scanned_pdf(pdf_bytes)

        # Get total page count
        from PyPDF2 import PdfReader
        try:
            reader = PdfReader(io.BytesIO(pdf_bytes))
            total_pages = len(reader.pages)
        except Exception as e:
            raise OCRException(f"Could not parse PDF for page count: {e}")

        # Check Limits
        if total_pages > settings.ocr_max_pages:
            raise OCRException(f"PDF exceeds maximum page limit of {settings.ocr_max_pages}. Pages: {total_pages}")

        # Render PDF pages to PIL images
        images = []
        try:
            from pdf2image import convert_from_bytes
            images = convert_from_bytes(pdf_bytes, dpi=settings.ocr_dpi)
        except Exception as e:
            raise OCRException(f"PDF rendering failed via pdf2image: {e}")

        if not images:
            raise OCRException("No pages rendered from PDF document")

        # Selective sampling for large docs
        use_sampling = total_pages > 50
        pages_processed_indices = []
        if use_sampling:
            logger.info(f"PDF page count ({total_pages}) > 50. Activating selective sampling (1 every {settings.ocr_large_doc_sample_rate} pages)")
            pages_processed_indices = list(range(0, total_pages, settings.ocr_large_doc_sample_rate))
        else:
            pages_processed_indices = list(range(total_pages))

        # Parallel page processing
        results = [None] * total_pages
        
        # Parallel Execution
        with concurrent.futures.ThreadPoolExecutor(max_workers=settings.ocr_parallel_workers) as executor:
            future_to_idx = {
                executor.submit(self._process_single_page, images[idx], idx, self.engine): idx
                for idx in pages_processed_indices
            }
            for future in concurrent.futures.as_completed(future_to_idx):
                idx = future_to_idx[future]
                try:
                    results[idx] = future.result()
                except Exception as exc:
                    logger.error(f"Page {idx} generated an exception: {exc}")
                    results[idx] = {
                        "text": "",
                        "confidence": 0.0,
                        "tables": [],
                        "engine_used": "none",
                        "error": str(exc),
                        "duration_ms": 0
                    }

        # Fill in skipped pages with placeholder text
        for idx in range(total_pages):
            if results[idx] is None:
                results[idx] = {
                    "text": f"[Page {idx + 1} skipped due to selective sampling of large document]",
                    "confidence": 1.0,
                    "tables": [],
                    "engine_used": "none",
                    "duration_ms": 0
                }

        # Aggregate Results
        full_text = "\n\n".join([r["text"] for r in results])
        per_page_text = [r["text"] for r in results]
        
        confidences = [r["confidence"] for idx, r in enumerate(results) if idx in pages_processed_indices and r["engine_used"] != "none"]
        avg_confidence = sum(confidences) / len(confidences) if confidences else 1.0

        all_tables = []
        for r in results:
            if r.get("tables"):
                all_tables.extend(r["tables"])

        engines = [r["engine_used"] for idx, r in enumerate(results) if idx in pages_processed_indices]
        engine_used = "cloud" if "cloud" in engines else ("tesseract" if "tesseract" in engines else "none")

        total_duration = int((time.time() - start_time) * 1000)

        # Apply Thresholds
        min_conf = settings.ocr_cloud_min_confidence if engine_used == "cloud" else settings.ocr_tesseract_min_confidence
        
        status = "success"
        if any(r.get("error") for r in results if r):
            status = "partial"

        ocr_result = {
            "text": full_text,
            "pages": per_page_text,
            "confidence": round(avg_confidence, 3),
            "engine_used": engine_used,
            "is_scanned": is_scanned,
            "tables": all_tables,
            "status": status,
            "duration_ms": total_duration
        }

        if is_scanned and avg_confidence < min_conf:
            logger.warning(f"OCR average confidence ({avg_confidence:.2f}) is below the required threshold of {min_conf:.2f}")
            raise LowConfidenceException(f"OCR confidence {avg_confidence:.2f} below threshold of {min_conf:.2f}", ocr_result)

        return ocr_result
