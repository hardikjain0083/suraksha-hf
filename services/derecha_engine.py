
import re
import hashlib
import numpy as np
from typing import List, Dict, Tuple, Optional
from dataclasses import dataclass, field
from collections import defaultdict
import difflib

# scikit-learn for TF-IDF
try:
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.metrics.pairwise import cosine_similarity
    SKLEARN_AVAILABLE = True
except ImportError:
    SKLEARN_AVAILABLE = False

# spaCy for dependency parsing
try:
    import spacy
    nlp = spacy.load("en_core_web_sm")
    SPACY_AVAILABLE = True
except (OSError, ImportError):
    nlp = None
    SPACY_AVAILABLE = False

# sentence-transformers for local embeddings (lazy import to avoid PyTorch DLL issues on Windows)
ST_AVAILABLE = True
try:
    import importlib.util
    spec = importlib.util.find_spec("sentence_transformers")
    if spec is None:
        ST_AVAILABLE = False
except (ImportError, ModuleNotFoundError):
    ST_AVAILABLE = False

SentenceTransformer = None  # Will be imported on demand

# WordNet for semantic similarity
try:
    from nltk.corpus import wordnet
    import nltk
    WORDNET_AVAILABLE = True
except ImportError:
    WORDNET_AVAILABLE = False

from config import (
    FINANCIAL_STOPWORDS, DIRECTIVE_KEYWORDS,
    CIRCULAR_PREFIX_MAP, MATCH_THRESHOLD, PARTIAL_THRESHOLD,
    SIGNAL_WEIGHTS, DEFAULT_EMBEDDING_MODEL
)

# =============================================================================
# DATA CLASSES
# =============================================================================

@dataclass
class SemanticFrame:
    """Represents a decomposed regulatory requirement or policy clause."""
    action: Optional[str] = None
    actor: Optional[str] = None
    object: Optional[str] = None
    constraint: Optional[str] = None
    time: Optional[str] = None
    reference: Optional[str] = None
    modality: str = "neutral"
    raw_text: str = ""
    
    def to_dict(self) -> Dict:
        return {
            "action": self.action,
            "actor": self.actor,
            "object": self.object,
            "constraint": self.constraint,
            "time": self.time,
            "reference": self.reference,
            "modality": self.modality,
            "raw_text": self.raw_text
        }


@dataclass
class ExtractedGuideline:
    """An RBI guideline extracted from a circular."""
    guideline_id: str
    text: str
    page_number: int
    semantic_frame: SemanticFrame
    keywords: List[str] = field(default_factory=list)
    directive_type: str = "neutral"
    applicability_tags: List[str] = field(default_factory=list)
    circular_number: Optional[str] = None
    circular_prefix: Optional[str] = None


@dataclass
class ExtractedClause:
    """A policy clause extracted from a bank policy document."""
    clause_id: str
    text: str
    page_number: int
    section_heading: Optional[str] = None
    semantic_frame: Optional[SemanticFrame] = None
    keywords: List[str] = field(default_factory=list)
    is_annexure: bool = False
    is_table: bool = False
    policy_name: Optional[str] = None


@dataclass
class GapResult:
    """Result of gap detection for a single guideline-clause pair."""
    gap_id: str
    rbi_guideline: ExtractedGuideline
    policy_clause: Optional[ExtractedClause]
    policy_id: Optional[str]
    policy_title: Optional[str]
    gap_type: str
    severity: str
    concerned_department: str
    mismatch_description: str
    similarity_scores: Dict[str, float] = field(default_factory=dict)
    final_score: float = 0.0
    status: str = "open"
    page_number: int = 0
    
    def to_dict(self) -> Dict:
        return {
            "gap_id": self.gap_id,
            "rbi_guideline": {
                "guideline_id": self.rbi_guideline.guideline_id,
                "text": self.rbi_guideline.text,
                "page_number": self.rbi_guideline.page_number,
                "semantic_frame": self.rbi_guideline.semantic_frame.to_dict(),
                "keywords": self.rbi_guideline.keywords,
                "directive_type": self.rbi_guideline.directive_type,
                "circular_number": self.rbi_guideline.circular_number,
                "circular_prefix": self.rbi_guideline.circular_prefix
            },
            "policy_clause": {
                "clause_id": self.policy_clause.clause_id if self.policy_clause else None,
                "text": self.policy_clause.text if self.policy_clause else None,
                "page_number": self.policy_clause.page_number if self.policy_clause else None,
                "section_heading": self.policy_clause.section_heading if self.policy_clause else None,
                "policy_name": self.policy_clause.policy_name if self.policy_clause else None
            } if self.policy_clause else None,
            "gap_type": self.gap_type,
            "severity": self.severity,
            "concerned_department": self.concerned_department,
            "mismatch_description": self.mismatch_description,
            "similarity_scores": self.similarity_scores,
            "final_score": round(self.final_score, 4),
            "status": self.status,
            "page_number": self.page_number
        }


# =============================================================================
# RAKE KEYWORD EXTRACTOR (Pure Python)
# =============================================================================

class RakeKeywordExtractor:
    """Pure Python implementation of RAKE algorithm."""
    
    def __init__(self, stopwords=None, min_length=1, max_length=10):
        self.stopwords = stopwords or set()
        self.min_length = min_length
        self.max_length = max_length
        self.delimiters = set(['.', ',', ';', ':', '!', '?', '(', ')', '[', ']',
                              '{', '}', '<', '>', '"', "'", '`', '|', '/',
                              '\\', '-', '_', '=', '+', '*', '&', '^', '%', '$',
                              '#', '@', '~'])
    
    def _is_stopword(self, word: str) -> bool:
        return word.lower() in self.stopwords or len(word) < 2
    
    def _split_to_phrases(self, text: str) -> List[List[str]]:
        """Split text into candidate phrases separated by stopwords/delimiters."""
        words = text.split()
        phrases = []
        current_phrase = []
        
        for word in words:
            clean_word = ''.join(c for c in word if c not in self.delimiters).lower()
            if self._is_stopword(clean_word) or not clean_word:
                if current_phrase:
                    phrases.append(current_phrase)
                    current_phrase = []
            else:
                current_phrase.append(clean_word)
        
        if current_phrase:
            phrases.append(current_phrase)
        
        return phrases
    
    def extract(self, text: str, top_k: int = 10) -> List[Tuple[str, float]]:
        """Extract top-k keywords with scores."""
        phrases = self._split_to_phrases(text)
        
        # Build word frequency and degree
        word_freq = defaultdict(int)
        word_deg = defaultdict(int)
        
        for phrase in phrases:
            phrase_len = len(phrase)
            for word in phrase:
                word_freq[word] += 1
                word_deg[word] += phrase_len
        
        # Calculate word scores
        word_scores = {}
        for word in word_freq:
            word_scores[word] = word_deg[word] / word_freq[word] if word_freq[word] > 0 else 0
        
        # Calculate phrase scores
        phrase_scores = []
        for phrase in phrases:
            if self.min_length <= len(phrase) <= self.max_length:
                score = sum(word_scores.get(word, 0) for word in phrase)
                phrase_text = ' '.join(phrase)
                phrase_scores.append((phrase_text, score))
        
        # Sort and deduplicate
        phrase_scores.sort(key=lambda x: x[1], reverse=True)
        seen = set()
        results = []
        for phrase, score in phrase_scores:
            if phrase not in seen:
                seen.add(phrase)
                results.append((phrase, score))
                if len(results) >= top_k:
                    break
        
        return results


# =============================================================================
# SEMANTIC FRAME EXTRACTOR (Rule-Based, DERECHA-Inspired)
# =============================================================================

class SemanticFrameExtractor:
    """Extracts semantic frames from regulatory text using rule-based approach."""
    
    def __init__(self):
        self.action_patterns = [
            r"(shall|must|should|are\s+directed\s+to|it\s+is\s+mandatory|"
            r"banks\s+are\s+required\s+to|shall\s+ensure|shall\s+maintain|"
            r"shall\s+submit|shall\s+furnish|shall\s+comply|shall\s+adhere|"
            r"shall\s+implement|shall\s+establish|shall\s+review|shall\s+formulate|"
            r"shall\s+put\s+in\s+place|shall\s+develop|shall\s+monitor|shall\s+report)",
        ]
        
        self.actor_patterns = [
            r"(banks|scheduled\s+commercial\s+banks|all\s+banks|lenders|"
            r"financial\s+institutions|regulated\s+entities|NBFCs|"
            r"primary\s+urban\s+co-operative\s+banks|payment\s+service\s+providers|"
            r"authorized\s+dealers|money\s+changers)",
        ]
        
        self.constraint_patterns = [
            r"(within\s+\d+\s+days|quarterly|annually|at\s+all\s+times|"
            r"not\s+less\s+than\s+\d+%|minimum\s+of\s+\d+|"
            r"not\s+exceeding\s+\d+|at\s+least\s+\d+|"
            r"not\s+more\s+than\s+\d+|maximum\s+of\s+\d+)",
        ]
        
        self.time_patterns = [
            r"(with\s+immediate\s+effect|effective\s+from|"
            r"by\s+\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|"
            r"within\s+\d+\s+days\s+of|not\s+later\s+than|"
            r"on\s+or\s+before|from\s+the\s+date\s+of)",
        ]
        
        self.reference_patterns = [
            r"(Circular\s+No\.\s+[A-Z0-9\-\./]+|"
            r"Master\s+Direction\s+[A-Z0-9\-\s]+|"
            r"Reserve\s+Bank\s+of\s+India\s+Act|"
            r"Section\s+\d+[A-Z]?\s+of\s+the|"
            r"RBI\s+Act\s*,\s*\d{4})",
        ]
        
        self.modality_patterns = {
            "must": r"\b(shall|must|are\s+directed\s+to|it\s+is\s+mandatory|"
                    r"compulsory|required\s+to|obligated\s+to|mandatory)\b",
            "should": r"\b(should|ought\s+to|recommended|advisable|"
                      r"advised\s+to|encouraged\s+to)\b",
            "may": r"\b(may|can|might|optional|at\s+the\s+discretion\s+of|"
                   r"if\s+deemed\s+fit|consider)\b",
        }
    
    def extract_frame(self, text: str) -> SemanticFrame:
        """Extract semantic frame from a regulatory text snippet."""
        frame = SemanticFrame(raw_text=text)
        lower_text = text.lower()
        
        # Extract modality
        for mod, pattern in self.modality_patterns.items():
            if re.search(pattern, lower_text):
                frame.modality = mod
                break
        
        # FIX: If no modality found, try to detect from policy language
        if frame.modality == "neutral":
            if "shall" in lower_text or "must" in lower_text:
                frame.modality = "must"
            elif "should" in lower_text:
                frame.modality = "should"
            elif "may" in lower_text:
                frame.modality = "may"
        
        # Extract action (main verb after directive keyword)
        for pattern in self.action_patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                start = match.end()
                remaining = text[start:].strip()
                words = remaining.split()[:5]
                for word in words:
                    clean = re.sub(r"[^a-zA-Z]", "", word).lower()
                    if clean and clean not in {"the", "a", "an", "to", "of", "in", "and", "that"}:
                        frame.action = clean
                        break
                break
        
        # FIX: Extract action from ANY sentence, not just after directives
        if not frame.action:
            # Use spaCy if available
            if SPACY_AVAILABLE and nlp:
                try:
                    doc = nlp(text[:200])  # First 200 chars
                    for token in doc:
                        if token.pos_ == "VERB" and token.dep_ in ["ROOT", "xcomp"]:
                            frame.action = token.lemma_
                            break
                except Exception:
                    pass  # spaCy parsing failed, use fallback
            
            # Fallback: find first meaningful verb
            if not frame.action:
                words = text.split()[:10]
                for word in words:
                    clean = re.sub(r"[^a-zA-Z]", "", word).lower()
                    if clean and len(clean) > 2 and clean not in {"the", "and", "for", "with", "that", "this", "bank", "banks"}:
                        # Check if it looks like a verb
                        if clean.endswith(('e', 's', 'd', 'ing')):
                            frame.action = clean
                            break
        
        # Extract actor
        for pattern in self.actor_patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                frame.actor = match.group(1)
                break
        
        # Extract object (text between action and constraint/time)
        if frame.action:
            action_idx = text.lower().find(frame.action)
            if action_idx >= 0:
                after_action = text[action_idx + len(frame.action):]
                end_idx = len(after_action)
                
                for pattern in self.constraint_patterns:
                    constraint_match = re.search(pattern, after_action, re.IGNORECASE)
                    if constraint_match:
                        end_idx = min(end_idx, constraint_match.start())
                
                for pattern in self.time_patterns:
                    time_match = re.search(pattern, after_action, re.IGNORECASE)
                    if time_match:
                        end_idx = min(end_idx, time_match.start())
                
                frame.object = after_action[:end_idx].strip()
                if len(frame.object) > 200:
                    frame.object = frame.object[:200] + "..."
        
        # Extract constraint
        for pattern in self.constraint_patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                frame.constraint = match.group(1)
                break
        
        # Extract time
        for pattern in self.time_patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                frame.time = match.group(1)
                break
        
        # Extract reference
        for pattern in self.reference_patterns:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                frame.reference = match.group(1)
                break
        
        return frame
    
    def compute_frame_matching_degree(self, frame1: SemanticFrame, frame2: SemanticFrame) -> float:
        """Compute matching degree between two semantic frames (0 to 1)."""
        if not frame1.action and not frame2.action:
            return 0.0
        
        # Predicate (action) matching
        predicate_match = False
        if frame1.action and frame2.action:
            if frame1.action.lower() == frame2.action.lower():
                predicate_match = True
            elif WORDNET_AVAILABLE:
                predicate_match = self._wu_palmer_match(frame1.action, frame2.action, threshold=0.9)
            else:
                predicate_match = difflib.SequenceMatcher(
                    None, frame1.action.lower(), frame2.action.lower()
                ).ratio() >= 0.85
        
        # Argument matching
        args1 = [frame1.actor, frame1.object, frame1.constraint, frame1.time]
        args2 = [frame2.actor, frame2.object, frame2.constraint, frame2.time]
        
        found_args = 0
        total_args = 0
        
        for a1, a2 in zip(args1, args2):
            if a1 or a2:
                total_args += 1
                if a1 and a2:
                    similarity = difflib.SequenceMatcher(None, a1.lower(), a2.lower()).ratio()
                    if similarity >= 0.7:
                        found_args += 1
                    elif a1.lower() in a2.lower() or a2.lower() in a1.lower():
                        found_args += 1
        
        # Matching degree formula from DERECHA
        matching_degree = (found_args + (1 if predicate_match else 0)) / (total_args + 1)
        return matching_degree
    
    def _wu_palmer_match(self, word1: str, word2: str, threshold: float = 0.9) -> bool:
        """Check if two words have Wu-Palmer similarity >= threshold."""
        if not WORDNET_AVAILABLE:
            return False
        
        synsets1 = wordnet.synsets(word1, pos=wordnet.VERB)
        synsets2 = wordnet.synsets(word2, pos=wordnet.VERB)
        
        if not synsets1 or not synsets2:
            return False
        
        max_sim = 0
        for s1 in synsets1:
            for s2 in synsets2:
                sim = s1.wup_similarity(s2)
                if sim and sim > max_sim:
                    max_sim = sim
        
        return max_sim >= threshold


# =============================================================================
# LOCAL LLM EMBEDDING GENERATOR (Vectorization ONLY)
# =============================================================================

class LocalEmbeddingGenerator:
    """Generates sentence embeddings using LOCAL models only."""
    
    def __init__(self, model_name: str = DEFAULT_EMBEDDING_MODEL):
        global SentenceTransformer
        
        if not ST_AVAILABLE:
            raise ImportError(
                "sentence-transformers not available. "
                "Install: pip install sentence-transformers"
            )
        
        # Lazy import of SentenceTransformer (avoid PyTorch DLL loading at startup)
        if SentenceTransformer is None:
            try:
                from sentence_transformers import SentenceTransformer as ST
                SentenceTransformer = ST
            except Exception as e:
                raise ImportError(
                    f"Failed to import sentence-transformers: {e}\n"
                    "This may be due to PyTorch DLL issues on Windows.\n"
                    "Try: pip install torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cpu"
                )
        
        self.model = SentenceTransformer(model_name)
        self.dimension = self.model.get_sentence_embedding_dimension()
    
    def encode(self, texts: List[str]) -> np.ndarray:
        """Generate embeddings for a list of texts."""
        return self.model.encode(texts, convert_to_numpy=True, show_progress_bar=False)
    
    def encode_single(self, text: str) -> np.ndarray:
        """Generate embedding for a single text."""
        return self.model.encode([text], convert_to_numpy=True)[0]
    
    def compute_similarity(self, text1: str, text2: str) -> float:
        """Compute cosine similarity between two texts."""
        embeddings = self.encode([text1, text2])
        return float(cosine_similarity([embeddings[0]], [embeddings[1]])[0][0])


# =============================================================================
# MAIN GAP DETECTOR CLASS
# =============================================================================

class GapDetector:
    """Main gap detection engine - DERECHA-inspired with local embeddings."""
    
    def __init__(self, embedding_model: str = DEFAULT_EMBEDDING_MODEL):
        self.rake = RakeKeywordExtractor(stopwords=FINANCIAL_STOPWORDS)
        self.frame_extractor = SemanticFrameExtractor()
        
        # Initialize embedding generator (gracefully fail if unavailable)
        self.embedding_generator = None
        if ST_AVAILABLE:
            try:
                self.embedding_generator = LocalEmbeddingGenerator(embedding_model)
            except ImportError as e:
                print(f"[WARNING] Embedding signal disabled: {e}")
        else:
            print("[INFO] sentence-transformers not available. Embedding signal disabled.")
        
        # TF-IDF vectorizer
        if SKLEARN_AVAILABLE:
            self.tfidf_vectorizer = TfidfVectorizer(
                stop_words="english",
                max_features=5000,
                ngram_range=(1, 2)
            )
        else:
            self.tfidf_vectorizer = None
            print("WARNING: scikit-learn not available. TF-IDF signal disabled.")
        
        self.signal_weights = SIGNAL_WEIGHTS.copy()
        self.MATCH_THRESHOLD = MATCH_THRESHOLD
        self.PARTIAL_THRESHOLD = PARTIAL_THRESHOLD
    
    def _split_circular_into_paragraphs(self, text: str) -> List[Tuple[int, str]]:
        """
        FIXED: Split circular text into individual directive paragraphs.
        Strategy:
        1. First try numbered paragraphs (1., 2., 3., etc.)
        2. Fallback to sentence splitting for directives
        3. Filter out non-directive paragraphs
        """
        paragraphs = []
        
        # Strategy 1: Split by numbered paragraphs (1., 2., 3., etc.)
        numbered_pattern = r"(?:^|\n)\s*(\d+(?:\.\d+)*)\s*[\.\)]\s+"
        matches = list(re.finditer(numbered_pattern, text))
        
        if len(matches) > 3:  # If we found enough numbered sections
            for i in range(len(matches)):
                start = matches[i].start()
                end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
                para_text = text[matches[i].end():end].strip()
                para_num = int(matches[i].group(1).split('.')[0])  # Get first number
                
                if len(para_text) > 30:
                    paragraphs.append((para_num, para_text))
        else:
            # Fallback: split by double newlines
            raw_paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
            for i, para in enumerate(raw_paras, 1):
                if len(para) > 30:
                    paragraphs.append((i, para))
        
        # Filter: Only keep paragraphs with directive keywords
        directive_paras = []
        for num, para in paragraphs:
            lower_para = para.lower()
            if any(keyword in lower_para for keyword in DIRECTIVE_KEYWORDS):
                cleaned = self._clean_paragraph(para)
                if len(cleaned) > 30:
                    directive_paras.append((num, cleaned))
        
        return directive_paras

    def _clean_paragraph(self, text: str) -> str:
        """Clean a paragraph by removing headers, footers, and extra whitespace."""
        text = re.sub(r"^\s*RESERVE\s+BANK\s+OF\s+INDIA\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"Yours\s+faithfully,.*", "", text, flags=re.IGNORECASE | re.DOTALL)
        text = re.sub(r"\(Chief\s+General\s+Manager\).*", "", text, flags=re.IGNORECASE | re.DOTALL)
        text = re.sub(r"Annexure\s+[I|II|III].*", "", text, flags=re.IGNORECASE | re.DOTALL)
        text = re.sub(r"\s+", " ", text).strip()
        return text

    def extract_guidelines_from_circular(self, circular_text: str,
                                         page_structure: List[Dict]) -> List[ExtractedGuideline]:
        """Extract guidelines from RBI circular text - FIXED VERSION."""
        guidelines = []
        
        # Combine all text from pages
        full_text = "\n\n".join([page.get("text_content", "") for page in page_structure])
        
        # Use the new splitting method
        directive_paras = self._split_circular_into_paragraphs(full_text)
        
        print(f"[INFO] Found {len(directive_paras)} directive paragraphs in circular")
        
        for idx, (para_num, para_text) in enumerate(directive_paras, 1):
            frame = self.frame_extractor.extract_frame(para_text)
            keywords = [kw for kw, score in self.rake.extract(para_text, top_k=10)]
            directive_type = self._classify_directive_type(para_text)
            
            guideline = ExtractedGuideline(
                guideline_id=f"GL_{idx:04d}",
                text=para_text,
                page_number=para_num,
                semantic_frame=frame,
                keywords=keywords,
                directive_type=directive_type
            )
            guidelines.append(guideline)
        
        return guidelines
    
    def extract_clauses_from_policy(self, policy_text: str,
                                     page_structure: List[Dict]) -> List[ExtractedClause]:
        """Extract clauses from bank policy document."""
        clauses = []
        clause_counter = 0
        
        for page in page_structure:
            page_num = page.get("page_number", 1)
            text = page.get("text_content", "")
            is_annexure = page.get("is_annexure", False)
            is_table = page.get("is_table", False)
            
            # FIX: Track line numbers for better page/section identification
            lines = text.split('\n')
            current_line = 0
            
            # Split by numbered sections or headings
            section_pattern = r"(?:^|\n)(?:\d+(?:\.\d+)*\s+|(?:Section|Annexure)\s+[A-Z0-9]+[\.\s])"
            sections = re.split(section_pattern, text)
            section_headings = re.findall(section_pattern, text)
            
            section_line_num = 0
            for i, section in enumerate(sections):
                if len(section.strip()) < 20:
                    continue
                
                # Track line number for this section
                section_lines = section.count('\n')
                current_line += section_lines
                
                clause_counter += 1
                heading = None
                if i > 0 and i - 1 < len(section_headings):
                    heading = section_headings[i - 1].strip()
                
                # Extract semantic frame
                frame = self.frame_extractor.extract_frame(section)
                
                # Extract keywords
                keywords = [kw for kw, score in self.rake.extract(section, top_k=10)]
                
                # FIX: Use line number as proxy for page for text files
                # For actual PDFs, page_num is accurate; for TXT files, use line numbers
                effective_page = page_num if page_num > 1 else max(1, current_line // 30)  # ~30 lines per "page"
                
                clause = ExtractedClause(
                    clause_id=f"CL_{clause_counter:04d}",
                    text=section.strip(),
                    page_number=effective_page,
                    section_heading=heading,
                    semantic_frame=frame,
                    keywords=keywords,
                    is_annexure=is_annexure,
                    is_table=is_table
                )
                clauses.append(clause)
        
        return clauses
    
    def _classify_directive_type(self, text: str) -> str:
        """Classify directive as mandatory, advisory, or information."""
        lower = text.lower()
        
        mandatory_markers = [
            "shall", "must", "are directed to", "it is mandatory",
            "compulsory", "required to", "obligated to", "mandatory"
        ]
        advisory_markers = [
            "should", "ought to", "recommended", "advisable",
            "encouraged to", "may consider", "advised to"
        ]
        
        if any(m in lower for m in mandatory_markers):
            return "mandatory"
        elif any(m in lower for m in advisory_markers):
            return "advisory"
        else:
            return "information"
    
    def compute_similarity_signals(self, guideline: ExtractedGuideline,
                                    clause: ExtractedClause) -> Dict[str, float]:
        """Compute all 5 similarity signals for a guideline-clause pair."""
        g_text = guideline.text
        c_text = clause.text
        
        signals = {}
        
        # Signal 1: TF-IDF Cosine Similarity
        if self.tfidf_vectorizer and SKLEARN_AVAILABLE:
            signals["tfidf"] = self._compute_tfidf_similarity(g_text, c_text)
        else:
            signals["tfidf"] = 0.0
        
        # Signal 2: RAKE Keyword Jaccard Similarity
        signals["jaccard"] = self._compute_jaccard_similarity(
            guideline.keywords, clause.keywords
        )
        
        # Signal 3: Fuzzy String Matching
        signals["fuzzy"] = difflib.SequenceMatcher(
            None, g_text.lower(), c_text.lower()
        ).ratio()
        
        # Signal 4: Local LLM Embedding Similarity
        if self.embedding_generator:
            try:
                signals["embedding"] = self.embedding_generator.compute_similarity(g_text, c_text)
            except Exception as e:
                print(f"[WARNING] Embedding computation failed: {e}")
                signals["embedding"] = 0.0
        else:
            signals["embedding"] = 0.0
        
        # Signal 5: Semantic Frame Matching Degree (DERECHA)
        if guideline.semantic_frame and clause.semantic_frame:
            signals["frame"] = self.frame_extractor.compute_frame_matching_degree(
                guideline.semantic_frame, clause.semantic_frame
            )
        else:
            signals["frame"] = 0.0
        
        return signals
    
    def _compute_tfidf_similarity(self, text1: str, text2: str) -> float:
        """Compute TF-IDF cosine similarity between two texts."""
        try:
            tfidf_matrix = self.tfidf_vectorizer.fit_transform([text1, text2])
            return float(cosine_similarity(tfidf_matrix[0:1], tfidf_matrix[1:2])[0][0])
        except Exception as e:
            print(f"[WARNING] TF-IDF computation failed: {e}")
            return 0.0
    
    def _compute_jaccard_similarity(self, keywords1: List[str],
                                     keywords2: List[str]) -> float:
        """Compute Jaccard similarity between two keyword sets."""
        set1 = set(k.lower() for k in keywords1)
        set2 = set(k.lower() for k in keywords2)
        
        # If one set is empty, check word-level overlap
        if not set1 or not set2:
            # Fall back to word-level Jaccard
            words1 = set()
            words2 = set()
            for k in keywords1:
                words1.update(k.lower().split())
            for k in keywords2:
                words2.update(k.lower().split())
            
            if not words1 or not words2:
                return 0.0
            
            intersection = len(words1 & words2)
            union = len(words1 | words2)
            return intersection / union if union > 0 else 0.0
        
        # Standard Jaccard on full keyword phrases
        intersection = len(set1 & set2)
        union = len(set1 | set2)
        phrase_score = intersection / union if union > 0 else 0.0
        
        # Also compute word-level Jaccard for robustness
        words1 = set()
        words2 = set()
        for k in keywords1:
            words1.update(k.lower().split())
        for k in keywords2:
            words2.update(k.lower().split())
        
        word_intersection = len(words1 & words2)
        word_union = len(words1 | words2)
        word_score = word_intersection / word_union if word_union > 0 else 0.0
        
        # Return the maximum of phrase-level and word-level Jaccard
        return max(phrase_score, word_score)
    
    def compute_final_score(self, signals: Dict[str, float]) -> float:
        """Compute weighted ensemble score from all signals."""
        final_score = 0.0
        for signal_name, weight in self.signal_weights.items():
            final_score += signals.get(signal_name, 0) * weight
        return final_score
    
    def detect_gaps(self, guidelines: List[ExtractedGuideline],
                    clauses: List[ExtractedClause],
                    policy_id: str,
                    policy_title: str) -> List[GapResult]:
        """Detect gaps between RBI guidelines and bank policy clauses."""
        gaps = []
        
        for guideline in guidelines:
            best_match = None
            best_score = 0.0
            best_signals = {}
            
            # Evaluate against ALL clauses
            for clause in clauses:
                signals = self.compute_similarity_signals(guideline, clause)
                score = self.compute_final_score(signals)
                
                if score > best_score:
                    best_score = score
                    best_match = clause
                    best_signals = signals
            
            # Determine gap type and severity
            gap_type, severity = self._classify_gap(guideline, best_match, best_score)
            
            # Determine concerned department
            department = self._route_to_department(guideline, best_match)
            
            # Generate mismatch description
            mismatch_desc = self._generate_mismatch_description(
                guideline, best_match, gap_type, best_score
            )
            
            gap = GapResult(
                gap_id=f"GAP_{hashlib.md5(guideline.text.encode()).hexdigest()[:8]}",
                rbi_guideline=guideline,
                policy_clause=best_match,
                policy_id=policy_id,
                policy_title=policy_title,
                gap_type=gap_type,
                severity=severity,
                concerned_department=department,
                mismatch_description=mismatch_desc,
                similarity_scores=best_signals,
                final_score=best_score,
                page_number=best_match.page_number if best_match else 0
            )
            gaps.append(gap)
        
        return gaps
    
    def _classify_gap(self, guideline: ExtractedGuideline,
                      best_clause: Optional[ExtractedClause],
                      score: float) -> Tuple[str, str]:
        """Classify gap type and severity."""
        
        # Check if this is an advisory directive (low severity regardless)
        if guideline.directive_type == "advisory":
            if score >= self.MATCH_THRESHOLD:
                return "matched", "none"
            elif score >= self.PARTIAL_THRESHOLD:
                return "insufficient_clause", "low"
            else:
                return "missing_clause", "low"
        
        if score >= self.MATCH_THRESHOLD:
            # Check for modality mismatch
            if (guideline.semantic_frame and best_clause and
                best_clause.semantic_frame):
                g_mod = guideline.semantic_frame.modality
                c_mod = best_clause.semantic_frame.modality
                
                if g_mod == "must" and c_mod in ["may", "neutral"]:
                    return "insufficient_clause", "critical"
                elif g_mod == "should" and c_mod == "may":
                    return "insufficient_clause", "high"
            
            return "matched", "none"
        
        elif score >= self.PARTIAL_THRESHOLD:
            return "insufficient_clause", "high"
        
        else:
            # No match found
            if best_clause is None:
                return "new_policy_required", "high"
            else:
                return "missing_clause", "critical"
    
    def _route_to_department(self, guideline: ExtractedGuideline,
                             best_clause: Optional[ExtractedClause]) -> str:
        """Route gap to appropriate department using multi-factor scoring."""
        dept_scores = defaultdict(float)
        guideline_text = guideline.text.lower()
        
        # Factor 1: Keyword density per department
        for dept, keywords in DEPT_KEYWORDS.items():
            for kw in keywords:
                if kw.lower() in guideline_text:
                    dept_scores[dept] += 1.0
        
        # Factor 2: Circular prefix hint
        if guideline.circular_prefix and guideline.circular_prefix in CIRCULAR_PREFIX_MAP:
            for dept in CIRCULAR_PREFIX_MAP[guideline.circular_prefix]["dept_hint"]:
                dept_scores[dept] += 2.0
        
        # Factor 3: Best matching clause department (if available)
        if best_clause and best_clause.policy_name:
            # Could be enhanced with policy metadata
            pass
        
        if not dept_scores:
            return "Compliance"  # Default fallback
        
        return max(dept_scores, key=dept_scores.get)
    
    def _generate_mismatch_description(self, guideline: ExtractedGuideline,
                                        clause: Optional[ExtractedClause],
                                        gap_type: str, score: float) -> str:
        """Generate human-readable mismatch description."""
        
        guideline_preview = guideline.text[:120] + "..." if len(guideline.text) > 120 else guideline.text
        clause_preview = clause.text[:120] + "..." if clause and len(clause.text) > 120 else (clause.text if clause else "N/A")
        
        descriptions = {
            "missing_clause": (
                f"RBI Circular directive: '{guideline_preview}' "
                f"has NO matching clause in the bank policy. "
                f"A new clause must be added to comply with this requirement. "
                f"Similarity score: {score:.2f}"
            ),
            "insufficient_clause": (
                f"RBI Circular directive: '{guideline_preview}' "
                f"is only PARTIALLY addressed in the bank policy. "
                f"Current policy clause: '{clause_preview}' "
                f"needs strengthening to match RBI requirements. "
                f"Similarity score: {score:.2f}"
            ),
            "outdated_clause": (
                f"RBI Circular directive: '{guideline_preview}' "
                f"CONTRADICTS the existing bank policy clause. "
                f"Policy must be updated to align with new RBI guidelines."
            ),
            "new_policy_required": (
                f"RBI Circular directive: '{guideline_preview}' "
                f"covers an area with NO EXISTING bank policy. "
                f"A new policy document must be created."
            ),
            "matched": (
                f"RBI Circular directive: '{guideline_preview}' "
                f"is FULLY MATCHED by existing policy clause. "
                f"No action required. Similarity score: {score:.2f}"
            )
        }
        
        return descriptions.get(gap_type, "Gap detected - manual review required.")
    
    def run_full_analysis(self, circular_doc, policy_doc) -> Dict:
        """Run complete gap analysis between circular and policy."""
        # Validate documents are not empty
        if not circular_doc.pages or all(not p.text_content.strip() for p in circular_doc.pages):
            raise ValueError("Circular document is empty or contains no readable text")
        if not policy_doc.pages or all(not p.text_content.strip() for p in policy_doc.pages):
            raise ValueError("Policy document is empty or contains no readable text")
        
        # Extract guidelines from circular
        circular_pages = [{"page_number": p.page_number, "text_content": p.text_content}
                         for p in circular_doc.pages]
        guidelines = self.extract_guidelines_from_circular(
            circular_doc.get_full_text(), circular_pages
        )
        
        # Add circular metadata to guidelines
        for g in guidelines:
            g.circular_number = circular_doc.circular_number
            g.circular_prefix = circular_doc.circular_prefix
        
        # Extract clauses from policy
        policy_pages = [{"page_number": p.page_number, "text_content": p.text_content,
                        "is_annexure": p.is_annexure, "is_table": p.is_table}
                       for p in policy_doc.pages]
        clauses = self.extract_clauses_from_policy(
            policy_doc.get_full_text(), policy_pages
        )
        
        # Set policy name for clauses
        for c in clauses:
            c.policy_name = policy_doc.filename
        
        # Detect gaps
        gaps = self.detect_gaps(
            guidelines, clauses,
            policy_id=policy_doc.filename,
            policy_title=policy_doc.filename
        )
        
        # Filter out matched items for the report
        actual_gaps = [g for g in gaps if g.gap_type != "matched"]
        
        return {
            "total_guidelines": len(guidelines),
            "total_clauses": len(clauses),
            "total_gaps": len(actual_gaps),
            "gaps": [g.to_dict() for g in actual_gaps],
            "summary": self._generate_summary(actual_gaps),
            "circular_info": {
                "filename": circular_doc.filename,
                "circular_number": circular_doc.circular_number,
                "circular_prefix": circular_doc.circular_prefix,
                "circular_date": circular_doc.circular_date
            },
            "policy_info": {
                "filename": policy_doc.filename,
                "total_pages": policy_doc.total_pages
            }
        }
    
    def _generate_summary(self, gaps: List[GapResult]) -> Dict:
        """Generate summary statistics for gaps."""
        severity_counts = defaultdict(int)
        dept_counts = defaultdict(int)
        gap_type_counts = defaultdict(int)
        
        for gap in gaps:
            severity_counts[gap.severity] += 1
            dept_counts[gap.concerned_department] += 1
            gap_type_counts[gap.gap_type] += 1
        
        return {
            "by_severity": dict(severity_counts),
            "by_department": dict(dept_counts),
            "by_gap_type": dict(gap_type_counts),
            "critical_count": severity_counts.get("critical", 0),
            "high_count": severity_counts.get("high", 0),
            "medium_count": severity_counts.get("medium", 0),
            "low_count": severity_counts.get("low", 0)
        }