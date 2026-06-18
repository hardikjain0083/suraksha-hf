import difflib

DIFF_STYLE = """
<style>
  .diff-container {
    overflow-x: auto;
    width: 100%;
    background-color: #0f172a;
    border: 1px solid #1e293b;
    border-radius: 8px;
    padding: 16px;
  }
  table.diff {
    font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace;
    border-collapse: collapse;
    width: 100%;
    font-size: 13px;
    color: #cbd5e1;
    background-color: #020617;
  }
  table.diff td {
    padding: 4px 8px;
    border: 1px solid #1e293b;
    white-space: pre-wrap;
    word-break: break-all;
  }
  .diff_header {
    background-color: #1e293b;
    color: #64748b;
    text-align: right;
    user-select: none;
    width: 40px;
    font-weight: 500;
  }
  .diff_next {
    background-color: #0f172a;
    color: #475569;
    width: 20px;
    text-align: center;
  }
  /* Additions (Green) */
  td.diff_add {
    background-color: #064e3b !important;
    color: #a7f3d0 !important;
  }
  span.diff_add {
    background-color: #047857 !important;
    color: #ffffff !important;
    padding: 1px 2px;
    border-radius: 2px;
  }
  /* Deletions (Red) */
  td.diff_sub {
    background-color: #7f1d1d !important;
    color: #fca5a5 !important;
  }
  span.diff_sub {
    background-color: #b91c1c !important;
    color: #ffffff !important;
    padding: 1px 2px;
    border-radius: 2px;
  }
  /* Modifications (Yellow) */
  td.diff_chg {
    background-color: #78350f !important;
    color: #fde68a !important;
  }
  span.diff_chg {
    background-color: #d97706 !important;
    color: #020617 !important;
    padding: 1px 2px;
    border-radius: 2px;
  }
</style>
"""

def generate_policy_diff_html(original_text: str, new_text: str) -> str:
    """
    Generate side-by-side HTML diff between original and new policy versions.
    Highlighted colors:
    - Additions: Green
    - Deletions: Red
    - Modifications: Yellow
    """
    original_lines = original_text.splitlines()
    new_lines = new_text.splitlines()
    
    differ = difflib.HtmlDiff(wrapcolumn=80)
    
    # Generate the table content
    diff_table = differ.make_table(
        original_lines, 
        new_lines, 
        fromdesc="Original Policy Version", 
        todesc="New Policy Version",
        context=True, # only show modified sections context
        numlines=3
    )
    
    # Wrap in styled container
    html_output = f"""
    <div class="diff-container">
        {DIFF_STYLE}
        {diff_table}
    </div>
    """
    return html_output
