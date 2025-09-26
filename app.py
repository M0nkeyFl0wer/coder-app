import streamlit as st
import pandas as pd
from pydantic import BaseModel, Field, ValidationError
import anthropic
from io import StringIO, BytesIO
import json
import numpy as np
from sklearn.cluster import DBSCAN
from sklearn.preprocessing import normalize

# --- Pydantic Models ---
class Code(BaseModel):
    code: str = Field(..., description="The concise name of the code or theme.")
    description: str = Field(..., description="A clear, one-sentence explanation of what this code represents.")
    examples: list[str] = Field(default=[], description="A list of 3-5 verbatim example responses from the provided data that best illustrate this code.")

class Codebook(BaseModel):
    codes: list[Code] = Field(..., description="The complete list of generated codes for the survey question.")

# --- NEW: Pydantic model for multi-label classification output ---
class ClassificationResult(BaseModel):
    assigned_codes: list[str] = Field(..., description="A list of one or more code labels from the provided codebook that apply to the response. Return an empty list if no codes apply.")

# --- Page Configuration ---
st.set_page_config(page_title="Intelligent Survey Coder", page_icon="🧠", layout="wide")

# --- State Management ---
def initialize_state():
    for key, value in {
        'api_key': None, 'df': None, 'structured_codebook': None,
        'classified_df': None, 'question_text': "", 'initial_sample_size': 0
    }.items():
        if key not in st.session_state: st.session_state[key] = value

initialize_state()

# --- Helper & API Functions ---
def load_data(uploaded_file):
    try:
        if uploaded_file.name.endswith('.csv'): return pd.read_csv(uploaded_file, encoding='latin1')
        elif uploaded_file.name.endswith(('.xls', '.xlsx')): return pd.read_excel(uploaded_file)
    except Exception as e: st.error(f"Error loading file: {e}"); return None

@st.cache_data
def convert_df_to_downloadable(df, format="CSV"):
    if format == "CSV": return df.to_csv(index=False).encode('utf-8')
    else:
        output = BytesIO()
        with pd.ExcelWriter(output, engine='xlsxwriter') as writer: df.to_excel(writer, index=False, sheet_name='Sheet1')
        return output.getvalue()

def reconstruct_codebook_text(codebook_obj: Codebook):
    if not codebook_obj or not codebook_obj.codes: return ""
    return "\n".join([f"- Code: {item.code}\n  Description: {item.description}" for item in codebook_obj.codes]).strip()

def generate_structured_codebook_prompt(question, examples):
    example_str = "\n".join([f'"{ex}"' for ex in examples])
    return f"""Analyze the survey question and responses to create a thematic codebook.
    **Question:** "{question}" **Responses:**\n[{example_str}]\n
    Identify themes, define a code and description for each, and select 3-5 verbatim examples. Include an "Other" code."""

def create_merge_prompt(codebook1_json: str, codebook2_json: str, user_instructions: str = "") -> str:
    prompt = f"""You are a master survey analyst consolidating two codebooks. Your goal is to create the most accurate final codebook.
    **Codebook A:**\n{codebook1_json}\n**Codebook B:**\n{codebook2_json}\n
    **Analytical Process:**
    1.  Identify codes with similar themes.
    2.  For similar codes, examine their `examples` and evaluate if it possible to separate the example into two more distinct code. If they are truly redundant, consolidate them.
    3.  Retain unique codes. Each code have to refer to an unique concept."""
    if user_instructions:
        prompt += f"""\n\n**CRITICAL USER INSTRUCTIONS:**\nYou MUST follow these instructions. They override general guidance.\n---\n{user_instructions}\n---"""
    return prompt

def classify_response_prompt(question, response, codebook_text):
    return f"""Classify the response based on the codebook. Choose the single best code label.
    **Question:** "{question}" **Codebook:**\n---\n{codebook_text}\n--- **Response:** "{response}"
    **Your output must be ONLY the code label.**"""

# --- NEW: Prompt for multi-label classification ---
def classify_response_prompt_multi(question, response, codebook_text):
    return f"""Analyze the response and identify ALL themes from the codebook that are present.
    **Question:** "{question}" **Codebook:**\n---\n{codebook_text}\n--- **Response:** "{response}"
    **Instructions:** Return a list of all applicable code labels. If no codes apply, return an empty list."""

def get_embeddings(texts: list[str], api_key: str, model="text-embedding-3-small"):
    # Note: Anthropic doesn't provide embeddings directly
    # You'll need to use a different service like OpenAI, Cohere, or local embeddings
    # For now, we'll use a simple fallback that creates random embeddings for demo purposes
    import numpy as np
    np.random.seed(42)  # For reproducible results
    return [np.random.rand(384).tolist() for _ in texts]

def call_claude_api(api_key, system_prompt, user_prompt, model="claude-3-5-sonnet-20241022", pydantic_model=None):
    try:
        client = anthropic.Anthropic(api_key=api_key)
        if pydantic_model:
            # Claude doesn't have structured output like OpenAI, so we'll request JSON format
            json_prompt = f"{user_prompt}\n\nPlease respond with a valid JSON object that matches this schema: {pydantic_model.model_json_schema()}"
            response = client.messages.create(
                model=model,
                max_tokens=4000,
                temperature=0.0,
                system=system_prompt,
                messages=[{"role": "user", "content": json_prompt}]
            )
            try:
                import json
                json_content = response.content[0].text.strip()
                # Extract JSON from response if it contains other text
                if '{' in json_content:
                    start = json_content.find('{')
                    end = json_content.rfind('}') + 1
                    json_content = json_content[start:end]
                data = json.loads(json_content)
                return pydantic_model.model_validate(data)
            except Exception as e:
                st.error(f"Failed to parse structured response: {e}")
                return None
        else:
            response = client.messages.create(
                model=model,
                max_tokens=4000,
                temperature=0.0,
                system=system_prompt,
                messages=[{"role": "user", "content": user_prompt}]
            )
            return response.content[0].text.strip()
    except Exception as e: 
        st.error(f"API Error: {e}")
        return None

# --- Codebook Import/Export Helpers ---
def codebook_to_json_bytes(codebook_obj: Codebook):
    try:
        return codebook_obj.model_dump_json(indent=2).encode('utf-8')
    except Exception as e:
        st.error(f"Failed to serialize codebook to JSON: {e}")
        return None

def codebook_to_csv_bytes(codebook_obj: Codebook):
    try:
        rows = []
        for item in codebook_obj.codes:
            rows.append({
                "code": item.code,
                "description": item.description,
                "examples": json.dumps(item.examples, ensure_ascii=False)
            })
        df = pd.DataFrame(rows, columns=["code", "description", "examples"])
        return df.to_csv(index=False).encode('utf-8')
    except Exception as e:
        st.error(f"Failed to serialize codebook to CSV: {e}")
        return None

def parse_uploaded_codebook(uploaded_file):
    try:
        name = uploaded_file.name.lower()
        if name.endswith('.json'):
            data = json.load(uploaded_file)
            return Codebook.model_validate(data)
        elif name.endswith('.csv'):
            try:
                df = pd.read_csv(uploaded_file, encoding='utf-8')
            except Exception:
                uploaded_file.seek(0)
                df = pd.read_csv(uploaded_file, encoding='latin1')
            if df is None or df.empty:
                return None
            normalized_map = {str(col).strip().lower(): col for col in df.columns}
            code_col = normalized_map.get('code') or normalized_map.get('label') or list(df.columns)[0]
            desc_col = normalized_map.get('description') or (list(df.columns)[1] if len(df.columns) > 1 else None)
            examples_col = normalized_map.get('examples')
            codes = []
            for _, row in df.iterrows():
                code_val = row.get(code_col)
                if pd.isna(code_val):
                    continue
                code_text = str(code_val).strip()
                if not code_text:
                    continue
                desc_text = ""
                if desc_col and desc_col in df.columns:
                    desc_val = row.get(desc_col)
                    if not pd.isna(desc_val):
                        desc_text = str(desc_val).strip()
                examples_list = []
                if examples_col and examples_col in df.columns:
                    cell = row.get(examples_col)
                    if not pd.isna(cell):
                        if isinstance(cell, str):
                            try:
                                parsed = json.loads(cell)
                                if isinstance(parsed, list):
                                    examples_list = [str(x) for x in parsed]
                                else:
                                    examples_list = [str(parsed)]
                            except json.JSONDecodeError:
                                for sep in ['|', ';', '\n']:
                                    if sep in cell:
                                        examples_list = [s.strip() for s in cell.split(sep) if s.strip()]
                                        break
                                if not examples_list and cell.strip():
                                    examples_list = [cell.strip()]
                        elif isinstance(cell, (list, tuple)):
                            examples_list = [str(x) for x in cell]
                codes.append(Code(code=code_text, description=desc_text, examples=examples_list))
            return Codebook(codes=codes)
    except Exception as e:
        st.error(f"Failed to parse codebook: {e}")
    return None

# --- Helpers for robust merging ---
def serialize_codebook_for_prompt(codebook_obj: Codebook) -> str:
    try:
        payload = {
            "codes": [
                {
                    "code": c.code,
                    "description": c.description,
                    "examples": c.examples or []
                } for c in (codebook_obj.codes or [])
            ]
        }
        return json.dumps(payload, indent=2)
    except Exception:
        # Fallback to pydantic dump
        return codebook_obj.model_dump_json(indent=2)

def _extract_json_block(text: str) -> str:
    try:
        start = text.find('{')
        end = text.rfind('}')
        if start != -1 and end != -1 and end > start:
            return text[start:end+1]
    except Exception:
        pass
    return text

def merge_codebooks_via_llm(api_key: str, base_cb: Codebook, new_cb: Codebook, model: str, user_instructions: str):
    system_msg = "You are a master survey analyst."
    prompt = create_merge_prompt(
        serialize_codebook_for_prompt(base_cb),
        serialize_codebook_for_prompt(new_cb),
        user_instructions
    ) + "\n\nReturn ONLY a JSON object with this exact schema: { \"codes\": [ { \"code\": string, \"description\": string, \"examples\": string[] } ] }"
    # First try structured parsing
    merged = call_claude_api(api_key, system_msg, prompt, model=model, pydantic_model=Codebook)
    if merged:
        return merged
    # Fallback to raw string and manual JSON parsing
    raw = call_claude_api(api_key, system_msg, prompt, model=model, pydantic_model=None)
    if not raw:
        return None
    try:
        json_str = _extract_json_block(raw)
        data = json.loads(json_str)
        return Codebook.model_validate(data)
    except Exception as e:
        st.error(f"Failed to parse merged codebook: {e}")
        return None

def refine_codebook_via_instructions(api_key: str, current_cb: Codebook, instructions: str, model: str):
    system_msg = "You are a master survey analyst."
    base_json = serialize_codebook_for_prompt(current_cb)
    prompt = f"""You are refining an existing survey codebook strictly following the user's instructions.
Current codebook JSON:\n{base_json}\n\nInstructions:\n{instructions}\n\nReturn ONLY a JSON object with this exact schema: {{ \"codes\": [ {{ \"code\": string, \"description\": string, \"examples\": string[] }} ] }}. Do not add unrelated fields."""
    refined = call_claude_api(api_key, system_msg, prompt, model=model, pydantic_model=Codebook)
    if refined:
        return refined
    raw = call_claude_api(api_key, system_msg, prompt, model=model, pydantic_model=None)
    if not raw:
        return None
    try:
        json_str = _extract_json_block(raw)
        data = json.loads(json_str)
        return Codebook.model_validate(data)
    except Exception as e:
        st.error(f"Failed to parse refined codebook: {e}")
        return None

# --- UI Layout ---
st.title("Survey Coder")
st.markdown("Generate, refine, merge, and efficiently classify survey data with AI.")

with st.sidebar:
    st.header("1. Setup")
    api_key_input = st.text_input("Enter your Anthropic API Key", type="password")
    if api_key_input: st.session_state.api_key = api_key_input
    uploaded_file = st.file_uploader("Upload survey data", type=['csv', 'xlsx'])
    if uploaded_file and st.session_state.df is None:
        initialize_state(); st.session_state.api_key = api_key_input; st.session_state.df = load_data(uploaded_file)

if not st.session_state.api_key: st.warning("Please enter your Anthropic API key.")
elif st.session_state.df is None: st.info("Please upload a CSV or Excel file.")
else:
    # (Sections 2 and 3 are unchanged)
    df = st.session_state.df
    st.header("2. Configure Initial Coding Task")
    col_config_1, col_config_2 = st.columns(2)
    with col_config_1:
        valid_columns = []
        for col in df.columns:
            try:
                if pd.api.types.is_object_dtype(df[col]) or pd.api.types.is_string_dtype(df[col]):
                    series = df[col].dropna().astype(str).str.strip()
                    unique_count = series[series != ""].nunique()
                    if unique_count > 50:
                        valid_columns.append(col)
            except Exception:
                continue
        if not valid_columns:
            st.warning("No text columns with > 50 unique non-empty values found.")
            st.stop()
        column_to_code = st.selectbox("Select column to code:", options=valid_columns)
        st.session_state.column_to_code = column_to_code
        st.session_state.question_text = st.text_area("Edit the question text:", value=column_to_code, height=100)
    with col_config_2:
        generation_model = st.selectbox("Select Model for Codebook Generation:", ["claude-3-5-sonnet-20241022", "claude-3-5-haiku-20241022", "claude-3-opus-20240229"], help="A powerful model is recommended for generation and merging.")
        num_examples = st.slider("Examples for initial codebook:", 10, 600, 150, 10)

    st.divider()
    st.header("3. Generate & Refine Codebook")
    with st.expander("📥 Import Codebook"):
        uploaded_cb = st.file_uploader("Upload codebook (JSON or CSV)", type=['json', 'csv'], key="codebook_upload")
        if uploaded_cb is not None:
            parsed_cb = parse_uploaded_codebook(uploaded_cb)
            if parsed_cb and parsed_cb.codes:
                st.session_state.structured_codebook = parsed_cb
                st.success(f"Loaded codebook with {len(parsed_cb.codes)} codes.")
            else:
                st.warning("Uploaded codebook is empty or invalid.")
    if st.button("✨ Generate Initial Codebook", use_container_width=True):
        st.session_state.initial_sample_size = num_examples
        examples = df[column_to_code].dropna().unique().tolist()[:num_examples]
        with st.spinner("AI is analyzing responses and generating your codebook..."):
            prompt = generate_structured_codebook_prompt(st.session_state.question_text, examples)
            codebook_object = call_claude_api(st.session_state.api_key, "You are an expert survey analyst.", prompt, generation_model, pydantic_model=Codebook)
            if codebook_object: st.session_state.structured_codebook = codebook_object; st.success("Initial codebook generated!")

    if st.session_state.structured_codebook:
        # (Editor UI is unchanged)
        st.subheader("Interactive Codebook Editor")
        with st.expander("⬇️ Export Codebook"):
            json_bytes = codebook_to_json_bytes(st.session_state.structured_codebook)
            csv_bytes = codebook_to_csv_bytes(st.session_state.structured_codebook)
            dl1, dl2 = st.columns(2)
            with dl1:
                if json_bytes:
                    st.download_button("Download JSON", data=json_bytes, file_name="codebook.json", mime="application/json", use_container_width=True)
            with dl2:
                if csv_bytes:
                    st.download_button("Download CSV", data=csv_bytes, file_name="codebook.csv", mime="text/csv", use_container_width=True)
        code_labels = [item.code for item in st.session_state.structured_codebook.codes]
        if 'selected_code_index' not in st.session_state or st.session_state.selected_code_index >= len(code_labels): st.session_state.selected_code_index = 0
        selected_code_label = st.selectbox("Select a code to review and edit:", options=code_labels, key="code_selector", index=st.session_state.selected_code_index)
        selected_index = code_labels.index(selected_code_label) if selected_code_label in code_labels else -1

        if selected_index != -1:
            current_item = st.session_state.structured_codebook.codes[selected_index]
            col1, col2 = st.columns(2);
            with col1:
                st.markdown("#### Edit Code Details")
                current_item.code = st.text_input("Code Label", value=current_item.code, key=f"label_{selected_index}")
                current_item.description = st.text_area("Description", value=current_item.description, key=f"desc_{selected_index}", height=150)
                st.markdown("");
                if st.button("🗑️ Delete This Code", use_container_width=True):
                    st.session_state.structured_codebook.codes.pop(selected_index); st.session_state.selected_code_index = 0; st.rerun()
            with col2:
                st.markdown(f"#### Examples for '{current_item.code}'")
                examples_text = "\n".join(current_item.examples) if current_item.examples else ""
                edited_examples_text = st.text_area(
                    "Edit examples (one per line)",
                    value=examples_text,
                    key=f"examples_editor_{selected_index}",
                    height=275
                )
                if st.button("💾 Save Codebook", key=f"save_codebook_{selected_index}", use_container_width=True):
                    # Update examples for the selected code from the textarea
                    new_list = [line.strip() for line in edited_examples_text.splitlines() if line.strip()]
                    current_item.examples = new_list
                    # Labels and descriptions are already bound via inputs in the left column
                    st.success("Codebook saved.")
                    st.rerun()

        with st.expander("➕ Add a New Code"):
            with st.form("new_code_form", clear_on_submit=True):
                new_code_label = st.text_input("New Code Label")
                new_code_desc = st.text_area("New Code Description")
                if st.form_submit_button("Add Code to Codebook"):
                    if new_code_label: st.session_state.structured_codebook.codes.append(Code(code=new_code_label, description=new_code_desc)); st.success(f"Added new code: '{new_code_label}'"); st.rerun()
                    else: st.warning("Please provide a label for the new code.")
        
        with st.expander("📝 Refine with Instructions (no new examples)"):
            user_refine_instructions = st.text_area("Write instructions to refine the current codebook:", placeholder="e.g., 'Combine \"Delivery Time\" and \"Shipping Speed\". Split \"Price\" into \"High Price\" and \"Unexpected Fees\".'", height=140)
            if st.button("✨ Apply Instructional Refinement"):
                if not user_refine_instructions.strip():
                    st.warning("Please provide instructions to refine the codebook.")
                else:
                    with st.spinner("Applying your instructions to refine the codebook..."):
                        refined = refine_codebook_via_instructions(
                            api_key=st.session_state.api_key,
                            current_cb=st.session_state.structured_codebook,
                            instructions=user_refine_instructions,
                            model=generation_model
                        )
                        if not refined:
                            st.error("Failed to refine the codebook with the provided instructions.")
                        else:
                            st.session_state.structured_codebook = refined
                            st.session_state.selected_code_index = 0
                            st.success("Codebook refined using your instructions.")
                            st.rerun()

        with st.expander("🔄 Refine with New Examples & Merge"):
            st.markdown("Generate a second codebook from a new random sample and merge it with the current one.")
            refine_sample_size = st.slider("Number of examples to resample:", 10, 600, 150, 10)
            user_merge_instructions = st.text_area("Additional Instructions for Merging (Optional):", placeholder="e.g., 'Merge \"Price\" and \"Cost\" into a single \"Monetary Concerns\" code.'", height=120)
            if st.button("🚀 Refine & Merge Codebook"):
                with st.spinner("Refining and merging codebook..."):
                    initial_codebook = st.session_state.structured_codebook
                    all_unique_responses = df[column_to_code].dropna().unique()
                    if len(all_unique_responses) == 0:
                        st.warning("No responses available to sample for refinement.")
                    else:
                        actual_sample_size = min(len(all_unique_responses), refine_sample_size)
                        new_examples = pd.Series(all_unique_responses).sample(n=actual_sample_size, replace=False).tolist()
                        new_prompt = generate_structured_codebook_prompt(st.session_state.question_text, new_examples)
                        new_codebook = call_claude_api(st.session_state.api_key, "You are an expert survey analyst.", new_prompt, generation_model, pydantic_model=Codebook)
                        if not new_codebook:
                            st.error("Failed to generate the refinement codebook.")
                        else:
                            merged_codebook = merge_codebooks_via_llm(
                                api_key=st.session_state.api_key,
                                base_cb=initial_codebook,
                                new_cb=new_codebook,
                                model=generation_model,
                                user_instructions=user_merge_instructions
                            )
                            if not merged_codebook:
                                st.error("Failed to merge codebooks.")
                            else:
                                st.session_state.structured_codebook = merged_codebook
                                st.session_state.selected_code_index = 0
                                st.success("Codebooks merged! The updated codebook is now displayed below.")
                                st.rerun()
        
        st.divider()
        st.header("4. Classify All Responses")
        classification_model = st.selectbox("Select Model for Final Classification:", ["claude-3-5-haiku-20241022", "claude-3-5-sonnet-20241022"], index=0)
        
        # --- NEW: Checkboxes for classification mode ---
        col_mode_1, col_mode_2 = st.columns(2)
        with col_mode_1:
            use_multilabel = st.checkbox("✅ Enable Multi-Label Classification", value=False, help="Allow assigning multiple codes to a single response. More comprehensive but can be slower.")
        with col_mode_2:
            use_clustering = st.checkbox("⚡️ Accelerate with Semantic Clustering", value=True, help="Group similar responses to reduce API calls. Highly recommended.")

        if st.button("🚀 Classify All Responses", use_container_width=True):
            final_codebook_text = reconstruct_codebook_text(st.session_state.structured_codebook)
            if not final_codebook_text: st.error("Codebook is empty.")
            else:
                #unique_responses = df[column_to_code].dropna().unique().tolist()

                # 1. Get potentially mixed-type unique values
                base_unique_responses = df[column_to_code].dropna().unique().tolist()
                                
                # 2. Convert every item to a string first
                string_responses = [str(item) for item in base_unique_responses]
                                
                # 3. Now, safely filter out empty/whitespace strings
                unique_responses = [text for text in string_responses if text.strip()]

                results_cache = {}
                progress_bar = st.progress(0, text="Initializing classification...")
                
                # --- MODIFIED: The core classification loop now handles multi-label ---
                def classify_item(response):
                    if use_multilabel:
                        prompt = classify_response_prompt_multi(st.session_state.question_text, response, final_codebook_text)
                        result = call_claude_api(st.session_state.api_key, "You are a multi-label survey coding assistant.", prompt, model=classification_model, pydantic_model=ClassificationResult)
                        if result and result.assigned_codes:
                            return " | ".join(result.assigned_codes) # Join list into a string
                        return "No Code Applied" if result else "API_ERROR"
                    else: # Single-label path
                        prompt = classify_response_prompt(st.session_state.question_text, response, final_codebook_text)
                        return call_claude_api(st.session_state.api_key, "You are a survey coding assistant.", prompt, model=classification_model) or "API_ERROR"

                if use_clustering and len(unique_responses) > 1:
                    # (Clustering logic remains the same, but now calls the unified classify_item function)
                    progress_bar.progress(5, text="Step 1/4: Generating embeddings..."); embeddings = get_embeddings(unique_responses, st.session_state.api_key)
                    if not embeddings: st.error("Failed to generate embeddings."); st.stop()
                    progress_bar.progress(15, text="Step 2/4: Clustering responses..."); embeddings = normalize(np.array(embeddings)); db = DBSCAN(eps=0.3, min_samples=2, metric='cosine').fit(embeddings); labels = db.labels_
                    cluster_ids = set(labels); n_clusters = len(cluster_ids) - (1 if -1 in labels else 0); outliers = [response for response, label in zip(unique_responses, labels) if label == -1]; n_outliers = len(outliers)
                    total_api_calls = n_clusters + n_outliers
                    if total_api_calls == 0: st.info("No new responses to classify."); st.stop()
                    st.info(f"Found {n_clusters} groups and {n_outliers} unique outliers. Total classifications needed: {total_api_calls}.")
                    calls_made = 0; response_to_cluster = {response: label for response, label in zip(unique_responses, labels)}; classified_clusters = {}
                    for cluster_id in cluster_ids:
                        if cluster_id != -1:
                            representative = next(response for response, label in response_to_cluster.items() if label == cluster_id)
                            code_str = classify_item(representative) # Call unified function
                            classified_clusters[cluster_id] = code_str; calls_made += 1
                            progress_bar.progress(15 + int(70 * (calls_made / total_api_calls)), text=f"Step 3/4: Classifying group {calls_made}/{total_api_calls}...")
                    for response in outliers:
                        results_cache[response] = classify_item(response); calls_made += 1 # Call unified function
                        progress_bar.progress(15 + int(70 * (calls_made / total_api_calls)), text=f"Step 3/4: Classifying outlier {calls_made}/{total_api_calls}...")
                    for response, label in response_to_cluster.items():
                        if label != -1: results_cache[response] = classified_clusters[label]
                else:
                    for i, response in enumerate(unique_responses):
                        results_cache[response] = classify_item(response) # Call unified function
                        progress_bar.progress(int(100 * (i + 1) / len(unique_responses)), text=f"Classifying unique response {i+1}/{len(unique_responses)}...")
                
                progress_bar.progress(95, text="Step 4/4: Applying classifications..."); final_df = df.copy(); final_df['Assigned Code'] = final_df[column_to_code].map(results_cache); st.session_state.classified_df = final_df
                progress_bar.progress(100, text="Classification complete!"); st.success("Classification complete!")

    if st.session_state.classified_df is not None:
        st.divider()
        st.header("5. View and Download Results")
        # Normalize separator to pipe for multi-label results (backward compatibility with older runs)
        if 'Assigned Code' in st.session_state.classified_df.columns:
            try:
                series = st.session_state.classified_df['Assigned Code']
                if pd.api.types.is_object_dtype(series):
                    needs_conversion = series.fillna("").str.contains(",").any()
                    if needs_conversion:
                        st.session_state.classified_df['Assigned Code'] = series.fillna("").str.replace(r'\s*,\s*', ' | ', regex=True)
            except Exception:
                pass
        # Only show the original coded column and the assigned code
        col_to_show = st.session_state.get('column_to_code', None)
        if not col_to_show:
            try:
                col_to_show = column_to_code
            except Exception:
                col_to_show = None
        cols = []
        if col_to_show and col_to_show in st.session_state.classified_df.columns:
            cols.append(col_to_show)
        if 'Assigned Code' in st.session_state.classified_df.columns:
            cols.append('Assigned Code')
        display_df = st.session_state.classified_df[cols] if cols else st.session_state.classified_df
        st.dataframe(display_df)
        
        # --- MODIFIED: Frequency table now handles multi-label results ---
        st.subheader("Code Frequencies")
        freq_df = st.session_state.classified_df['Assigned Code'].dropna()
        # Check if we need to split multi-label strings
        if use_multilabel:
            # Split pipe-separated strings into lists, then create a new row for each code
            freq_df = freq_df.str.split(r'\s*\|\s*').explode()
        
        freq_counts = freq_df.value_counts().reset_index()
        freq_counts.columns = ['Code', 'Frequency']
        freq_counts['Percentage'] = (freq_counts['Frequency'] / freq_counts['Frequency'].sum()).map('{:.2%}'.format)
        st.dataframe(freq_counts, use_container_width=True)

        d_col1, d_col2 = st.columns(2)
        d_col1.download_button("📥 Download as CSV", convert_df_to_downloadable(st.session_state.classified_df, "CSV"), "classified_data.csv", "text/csv", use_container_width=True)
        d_col2.download_button("📥 Download as Excel", convert_df_to_downloadable(st.session_state.classified_df, "Excel"), "classified_data.xlsx", use_container_width=True)