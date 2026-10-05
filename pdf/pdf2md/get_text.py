"""
PDF to Markdown/LaTeX Extractor
Uses marker-pdf for accurate extraction of mathematical content from PDFs
Output: Markdown files with preserved LaTeX formulas
"""

import os
import yaml
import time
import csv
import argparse
from pathlib import Path
from datetime import datetime


def parse_args():
    """Parse optional CLI arguments."""
    parser = argparse.ArgumentParser(
        description="Extract text/markdown from PDFs with method fallbacks."
    )
    parser.add_argument(
        "--pdf",
        type=str,
        default=None,
        help="Process only one PDF (filename like reyes2025.pdf or absolute/relative path).",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=None,
        help="Override marker-pdf timeout in seconds for this run.",
    )
    return parser.parse_args()

def load_config():
    """Load configuration from config.yml"""
    config_path = Path(__file__).parent / "config.yml"
    with open(config_path, 'r', encoding='utf-8') as f:
        config = yaml.safe_load(f)
    return config, config_path


def resolve_path(value, base_dir):
    """Resolve config paths relative to config file location (not CWD)."""
    p = Path(value)
    if not p.is_absolute():
        p = (base_dir / p).resolve()
    return p

def get_log_file_path():
    """Get the path to the conversion log file"""
    return Path(__file__).parent / "conversion_log.csv"

def log_conversion(pdf_name, method, duration, file_size_mb, success, output_size):
    """Log conversion details to CSV file"""
    log_file = get_log_file_path()
    file_exists = log_file.exists()

    # NOTE: On some Windows setups, passing a Path object to open() can
    # intermittently raise OSError(22). Converting to str() avoids this.
    try:
        with open(str(log_file), 'a', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            if not file_exists:
                writer.writerow(['timestamp', 'pdf_name', 'method', 'duration_sec', 'file_size_mb', 'success', 'output_size'])
            writer.writerow([
                datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                pdf_name,
                method,
                f"{duration:.2f}",
                f"{file_size_mb:.2f}",
                success,
                output_size
            ])
    except OSError as e:
        # Logging must never prevent conversion.
        print(f"  ⚠️  WARNING: Could not write log file: {log_file} ({e})")

def get_average_duration(method_name):
    """Get average duration for a specific method from log file"""
    log_file = get_log_file_path()
    if not log_file.exists():
        return None
    
    durations = []
    try:
        with open(str(log_file), 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row['method'] == method_name and row['success'] == 'True':
                    durations.append(float(row['duration_sec']))
    except Exception:
        return None
    
    if durations:
        return sum(durations) / len(durations)
    return None

def get_logged_files():
    """Get set of PDFs already logged successfully"""
    log_file = get_log_file_path()
    if not log_file.exists():
        return {}
    
    logged = {}  # pdf_name -> {method, timestamp}
    try:
        with open(str(log_file), 'r', encoding='utf-8') as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row['success'] == 'True':
                    pdf_name = row['pdf_name']
                    method = row['method']
                    # Keep the most recent successful conversion for each PDF+method
                    key = (pdf_name, method)
                    if key not in logged or row['timestamp'] > logged[key]['timestamp']:
                        logged[key] = {'timestamp': row['timestamp'], 'method': method}
    except Exception:
        return {}
    
    return logged

def initialize_log_from_existing_files(txt_dir, pdf_dir, extraction_methods):
    """Scan existing output files and add them to log if not present"""
    log_file = get_log_file_path()
    logged_files = get_logged_files()
    
    print(f"\n🔍 Scanning existing output files...")
    new_entries = 0
    
    for method_name, _, ext, method_dir in extraction_methods:
        method_path = txt_dir / method_dir
        if not method_path.exists():
            continue

        # marker-pdf outputs are usually directories (one per PDF) that contain
        # <pdf_stem>.md, <pdf_stem>_meta.json and extracted images.
        # Some PDF stems contain dots (e.g. "1-s2.0-...") so using Path.stem on
        # the directory name would truncate it and mis-log the PDF name.
        if method_dir == "marker-pdf" and ext == "":
            for item in method_path.iterdir():
                if item.is_dir():
                    pdf_base = item.name
                    pdf_name = f"{pdf_base}.pdf"
                elif item.is_file() and item.suffix.lower() == ".md":
                    # Older/alternate marker behavior: flat .md in output dir
                    pdf_base = item.stem
                    pdf_name = f"{pdf_base}.pdf"
                else:
                    continue

                if (pdf_name, method_name) in logged_files:
                    continue

                pdf_path = pdf_dir / pdf_name
                if pdf_path.exists():
                    file_size_mb = pdf_path.stat().st_size / (1024 * 1024)
                else:
                    file_size_mb = 0.0

                # Best-effort output sizing
                output_size = 0
                if item.is_dir():
                    preferred_md = item / f"{pdf_base}.md"
                    if preferred_md.exists():
                        output_size = preferred_md.stat().st_size
                    else:
                        md_candidates = list(item.glob("*.md"))
                        if len(md_candidates) == 1:
                            output_size = md_candidates[0].stat().st_size
                else:
                    output_size = item.stat().st_size

                # Add to log with estimated duration (0 since we don't know)
                log_conversion(pdf_name, method_name, 0.0, file_size_mb, True, output_size)
                new_entries += 1
                print(f"  + Added to log: {pdf_name} -> {method_dir}/")

            continue
        
        # Get all output files for this method
        pattern = f"*{ext}"
        for output_file in method_path.glob(pattern):
            # Get corresponding PDF name
            pdf_name = output_file.stem + ".pdf"
            pdf_path = pdf_dir / pdf_name
            
            # Check if already logged
            if (pdf_name, method_name) in logged_files:
                continue
            
            # Check if PDF exists to get file size
            if pdf_path.exists():
                file_size_mb = pdf_path.stat().st_size / (1024 * 1024)
            else:
                file_size_mb = 0.0
            
            # Get output size
            output_size = output_file.stat().st_size
            
            # Add to log with estimated duration (0 since we don't know)
            log_conversion(pdf_name, method_name, 0.0, file_size_mb, True, output_size)
            new_entries += 1
            print(f"  + Added to log: {pdf_name} -> {method_dir}/")
    
    if new_entries > 0:
        print(f"✓ Added {new_entries} existing file(s) to log\n")
    else:
        print(f"✓ All existing files already in log\n")
    
    return new_entries

def format_duration(seconds):
    """Format duration in human-readable format"""
    if seconds < 60:
        return f"{seconds:.1f}s"
    elif seconds < 3600:
        mins = int(seconds // 60)
        secs = seconds % 60
        return f"{mins}m {secs:.0f}s"
    else:
        hours = int(seconds // 3600)
        mins = int((seconds % 3600) // 60)
        return f"{hours}h {mins}m"

def extract_with_marker(pdf_path, output_path, timeout=300):
    """
    Extract PDF to Markdown using marker-pdf (best for mathematical PDFs)
    Requires: pip install marker-pdf
    """
    try:
        # marker-pdf v1.10+ uses CLI tool, run it as subprocess
        import subprocess
        import sys
        import shutil
        
        # Find marker_single command
        marker_cmd = shutil.which("marker_single")
        if not marker_cmd:
            # When running from a venv on Windows, sys.executable is typically
            # <venv>\Scripts\python.exe and console scripts (marker_single.exe)
            # live right next to it.
            python_dir = Path(sys.executable).parent
            candidates = [
                python_dir / "marker_single.exe",
                python_dir / "Scripts" / "marker_single.exe",
            ]

            for candidate in candidates:
                if candidate.exists():
                    marker_cmd = str(candidate)
                    break

            if not marker_cmd:
                return False, "marker_single not found (is marker-pdf installed in this environment?)"
        
        # marker_single syntax: marker_single [OPTIONS] FPATH
        # NOTE: marker writes a subfolder per PDF inside --output_dir.
        output_root = Path(output_path).parent
        result = subprocess.run(
            [str(marker_cmd), pdf_path, "--output_dir", str(output_root)],
            capture_output=True,
            text=True,
            timeout=timeout
        )
        
        if result.returncode == 0:
            pdf_name = Path(pdf_path).stem

            # Preferred (current marker behavior): <output_root>/<pdf_name>/<pdf_name>.md
            marker_dir = output_root / pdf_name
            marker_md = marker_dir / f"{pdf_name}.md"

            if marker_md.exists():
                return True, marker_md.stat().st_size

            # Older/alternate behavior: <output_root>/<pdf_name>.md
            marker_md_flat = output_root / f"{pdf_name}.md"
            if marker_md_flat.exists():
                return True, marker_md_flat.stat().st_size

            # As a last resort, search for a single .md inside the expected folder.
            if marker_dir.exists() and marker_dir.is_dir():
                md_candidates = list(marker_dir.glob("*.md"))
                if len(md_candidates) == 1:
                    return True, md_candidates[0].stat().st_size

            return False, f"marker completed but output not found in: {output_root}"

        error_text = result.stderr or result.stdout or "unknown error"
        error_text = error_text.strip()
        # Keep the console readable but include enough context to debug.
        if len(error_text) > 4000:
            error_text = error_text[:4000] + "..."
        return False, f"marker failed: {error_text}"
    
    except FileNotFoundError:
        return False, "marker_single command not found"
    except subprocess.TimeoutExpired:
        timeout_mins = timeout / 60
        return False, f"marker timed out (>{timeout_mins:.0f} min)"
    except Exception as e:
        return False, str(e)

def extract_with_pymupdf4llm(pdf_path, output_path):
    """
    Fallback: Extract PDF to Markdown using pymupdf4llm
    Requires: pip install pymupdf4llm
    Better than basic text extraction, but less accurate than marker
    """
    try:
        import pymupdf4llm
        
        # Convert PDF to Markdown
        md_text = pymupdf4llm.to_markdown(pdf_path)
        
        # Save as Markdown
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(md_text)
        
        return True, len(md_text)
    
    except ImportError:
        return False, "pymupdf4llm not installed"
    except Exception as e:
        return False, str(e)

def extract_basic(pdf_path, output_path):
    """
    Last resort: Basic extraction using PyMuPDF
    Requires: pip install PyMuPDF
    """
    try:
        import fitz  # PyMuPDF
        
        doc = fitz.open(pdf_path)
        text = []
        
        for page_num, page in enumerate(doc, 1):
            text.append(f"# Page {page_num}\n\n")
            text.append(page.get_text("text"))
            text.append("\n\n")
        
        doc.close()
        
        full_text = "".join(text)
        
        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(full_text)
        
        return True, len(full_text)
    
    except ImportError:
        return False, "PyMuPDF not installed"
    except Exception as e:
        return False, str(e)

def check_method_availability():
    """Check which extraction methods are available"""
    import sys
    import shutil
    available = []
    
    # Check marker-pdf (CLI tool)
    try:
        marker_cmd = shutil.which("marker_single")
        if not marker_cmd:
            python_dir = Path(sys.executable).parent
            candidates = [
                python_dir / "marker_single.exe",
                python_dir / "Scripts" / "marker_single.exe",
            ]

            for candidate in candidates:
                if candidate.exists():
                    marker_cmd = str(candidate)
                    break

        if marker_cmd:
            # marker writes a directory per PDF (containing <stem>.md + images)
            available.append(("marker-pdf (BEST for math)", extract_with_marker, "", "marker-pdf"))
    except Exception:
        pass
    
    # Check pymupdf4llm
    try:
        import pymupdf4llm
        available.append(("pymupdf4llm (GOOD)", extract_with_pymupdf4llm, ".md", "pymupdf4llm"))
    except (ImportError, Exception):
        pass
    
    # Check PyMuPDF (fallback)
    try:
        import fitz
        available.append(("PyMuPDF basic (FALLBACK)", extract_basic, ".txt", "pymupdf-basic"))
    except (ImportError, Exception):
        pass
    
    return available

def main():
    args = parse_args()

    # Load configuration
    config, config_path = load_config()
    base_dir = config_path.parent
    pdf_dir = resolve_path(config['pdf_dir'], base_dir)
    txt_dir = resolve_path(config['txt_dir'], base_dir)
    timeout = config.get('single_pdf_timeout', 300)
    if args.timeout is not None:
        timeout = args.timeout
    retry_fallback = config.get('retry_fallback', False)
    
    # Create output directory
    txt_dir.mkdir(parents=True, exist_ok=True)
    start_time = datetime.now()
    print(f"[{start_time.strftime('%H:%M:%S')}] Starting PDF extraction")
    print(f"Output directory: {txt_dir}")
    print(f"Log file: {get_log_file_path()}")
    if retry_fallback:
        print(f"Mode: RETRY FALLBACK (will reprocess fallback conversions)")
    
    # Get PDF files
    if args.pdf:
        single_pdf = Path(args.pdf)
        if not single_pdf.is_absolute():
            candidate_in_pdf_dir = pdf_dir / args.pdf
            if candidate_in_pdf_dir.exists():
                single_pdf = candidate_in_pdf_dir
            else:
                single_pdf = Path(args.pdf)

        if not single_pdf.exists() and single_pdf.suffix.lower() != ".pdf":
            candidate_with_suffix = Path(f"{single_pdf}.pdf")
            if candidate_with_suffix.exists():
                single_pdf = candidate_with_suffix
            else:
                candidate_in_pdf_dir_with_suffix = pdf_dir / f"{args.pdf}.pdf"
                if candidate_in_pdf_dir_with_suffix.exists():
                    single_pdf = candidate_in_pdf_dir_with_suffix

        if not single_pdf.exists():
            print(f"PDF not found: {args.pdf}")
            return

        pdf_files = [single_pdf]
        print(f"Single-file mode: {single_pdf}")
    else:
        pdf_files = list(pdf_dir.glob("*.pdf"))
    
    if not pdf_files:
        print(f"No PDF files found in {pdf_dir}")
        return
    
    print(f"\nFound {len(pdf_files)} PDF file(s)")
    print("=" * 60)
    
    # Check which extraction methods are available
    extraction_methods = check_method_availability()
    
    if not extraction_methods:
        print("\n✗ ERROR: No extraction libraries found!")
        print("Run: python setup_pdf_extraction.py")
        return
    
    # Initialize log from existing files
    initialize_log_from_existing_files(txt_dir, pdf_dir, extraction_methods)
    
    print(f"\n📚 Available extraction methods:")
    for method_name, _, _, _ in extraction_methods:
        avg_duration = get_average_duration(method_name)
        if avg_duration:
            print(f"  ✓ {method_name} (avg: {format_duration(avg_duration)}/file)")
        else:
            print(f"  ✓ {method_name}")
    
    if len(extraction_methods) == 1 and "FALLBACK" in extraction_methods[0][0]:
        print("\n⚠️  WARNING: Only basic extraction available!")
        print("   For better LaTeX/formula extraction, install:")
        print("   pip install marker-pdf     (BEST - most accurate)")
        print("   pip install pymupdf4llm    (GOOD - faster)")
    
    primary_method = extraction_methods[0][0]
    print(f"\nUsing: {primary_method}")
    
    # Estimate total time if we have historical data
    avg_duration = get_average_duration(primary_method)
    if avg_duration:
        estimated_total = avg_duration * len(pdf_files)
        print(f"Estimated time: {format_duration(estimated_total)} (based on previous conversions)")
    
    print("=" * 60 + "\n")
    
    # Process each PDF
    processed = 0
    retried = 0
    for idx, pdf_file in enumerate(pdf_files, 1):
        # Generate output filename (try .md first, fallback to .txt)
        base_name = pdf_file.stem
        file_size_mb = pdf_file.stat().st_size / (1024 * 1024)
        
        current_time = datetime.now()
        print(f"[{current_time.strftime('%H:%M:%S')}] [{idx}/{len(pdf_files)}] Processing: {pdf_file.name} ({file_size_mb:.2f} MB)")
        
        # Check if file was processed with fallback method and retry mode is on
        primary_method = extraction_methods[0] if extraction_methods else None
        needs_retry = False
        
        if retry_fallback and primary_method:
            _, _, _, primary_dir = primary_method
            primary_output_dir = txt_dir / primary_dir
            primary_exists = False
            
            # Check if primary output exists
            for _, _, ext, method_dir in extraction_methods:
                if method_dir == primary_dir:
                    primary_file = primary_output_dir / f"{base_name}{ext}"
                    if primary_file.exists():
                        primary_exists = True
                        break
            
            # Check if fallback output exists
            fallback_exists = False
            if not primary_exists:
                for method_name, _, ext, method_dir in extraction_methods[1:]:  # Skip primary
                    fallback_file = (txt_dir / method_dir) / f"{base_name}{ext}"
                    if fallback_file.exists():
                        fallback_exists = True
                        print(f"  🔄 RETRY: Found in {method_dir}/, attempting with {primary_method[0]}")
                        needs_retry = True
                        retried += 1
                        break
        
        # Try each method until one succeeds
        success = False
        for method_idx, (method_name, method_func, ext, method_dir) in enumerate(extraction_methods):
            # In retry mode, only try primary method for files that need retry
            if needs_retry and method_idx > 0:
                # Skip non-primary methods when retrying
                continue
            
            # Create method-specific subdirectory
            method_output_dir = txt_dir / method_dir
            method_output_dir.mkdir(parents=True, exist_ok=True)
            
            output_file = method_output_dir / f"{base_name}{ext}"
            
            # Skip if already exists (unless retry mode and this is primary method)
            if output_file.exists():
                if needs_retry and method_idx == 0:
                    # This is retry mode and we're trying the primary method
                    pass  # Don't skip, process it
                else:
                    print(f"  ✓ SKIP: Already exists - {method_dir}/{output_file.name}")
                    success = True
                    break
            
            # Try extraction with timing
            extraction_start = time.time()
            # Pass timeout parameter to marker function
            if method_name.startswith("marker-pdf"):
                result, info = method_func(str(pdf_file), str(output_file), timeout=timeout)
            else:
                result, info = method_func(str(pdf_file), str(output_file))
            extraction_duration = time.time() - extraction_start
            
            if result:
                print(f"  ✓ SUCCESS using {method_name}: {method_dir}/{output_file.name} ({info} chars) - {format_duration(extraction_duration)}")
                log_conversion(pdf_file.name, method_name, extraction_duration, file_size_mb, True, info)
                success = True
                processed += 1
                
                # Show estimated time remaining
                if processed > 0 and idx < len(pdf_files):
                    remaining = len(pdf_files) - idx
                    avg = get_average_duration(method_name)
                    if avg:
                        est_remaining = avg * remaining
                        print(f"  ⏱️  Estimated time remaining: {format_duration(est_remaining)}")
                break
            else:
                # Method failed, show error and try next
                print(f"  ✗ FAILED {method_name}: {info} - {format_duration(extraction_duration)}")
                log_conversion(pdf_file.name, method_name, extraction_duration, file_size_mb, False, 0)
                continue
        
        if not success:
            print(f"  ✗ ERROR: All extraction methods failed for {pdf_file.name}")
            print(f"    Install required packages: pip install marker-pdf pymupdf4llm PyMuPDF")
    
    # Final summary
    end_time = datetime.now()
    total_duration = (end_time - start_time).total_seconds()
    
    print("\n" + "=" * 60)
    print(f"[{end_time.strftime('%H:%M:%S')}] Conversion complete!")
    print(f"Total time: {format_duration(total_duration)}")
    print(f"Processed: {processed}/{len(pdf_files)} files")
    if retry_fallback and retried > 0:
        print(f"Retried: {retried} file(s) from fallback methods")
    print(f"\nOutputs organized by method in: {txt_dir}")
    for method_name, _, _, method_dir in extraction_methods:
        method_path = txt_dir / method_dir
        if method_path.exists():
            file_count = len(list(method_path.glob("*")))
            if file_count > 0:
                print(f"  - {method_dir}/  ({file_count} files)")
    print(f"\nLog saved to: {get_log_file_path()}")
    print("\nFor best results with mathematical PDFs, install:")
    print("  pip install marker-pdf")
    print("\nAlternatively (faster but less accurate):")
    print("  pip install pymupdf4llm")

if __name__ == "__main__":
    main()
