# Qwen3-Embedding-4B vs Gemini Embedding 001：官方來源查核

日期：2026-08-25
範圍：只採 Qwen、Google Gemini 與 llama.cpp 的第一方來源；本文不代表實機品質測試結果。

## 一句話結論

兩者可以做公平的同資料對照，但不能只看公開排行榜。正式測試至少要固定同一批文件、切塊、問題、正確答案、向量維度與搜尋方法；同時正確套用 Gemini 的 task type，以及 Qwen 的 query instruction、last-token pooling 與 L2 normalization。Qwen 官方公開表格比較的是 `gemini-embedding-exp-03-07`，不是本案現行的 `gemini-embedding-001`，所以公開分數無法代替本案實測。

## 1. Qwen3-Embedding-4B 官方規格

### 基本能力與維度

- 模型用途：文字 embedding；參數量 4B。
- 語言：官方標示支援 100+ 語言，包含多種程式語言。
- 上下文長度：32K。
- 原生最大向量維度：2560。
- 支援 MRL 自訂輸出維度，官方標示範圍為 32～2560 維。
- 支援 task／language／scenario 專用 instruction。
- Hugging Face model card 標示 Apache-2.0 license。

來源：

- Qwen 官方模型卡：<https://huggingface.co/Qwen/Qwen3-Embedding-4B>
- Qwen 官方 GitHub：<https://github.com/QwenLM/Qwen3-Embedding>

### Query、document、pooling 與 normalization

Qwen 官方範例的檢索方式不是把 query 和 document 原封不動地用同一格式送入：

- query 前面加一行任務 instruction，格式為 `Instruct: ...\nQuery: ...`；
- retrieval document 不加 instruction；
- Transformers 範例採 last-token pooling；
- 輸出以 L2／Euclidean norm 正規化，再用內積（等同 normalized vectors 的 cosine similarity）比較；
- 官方表示，多數檢索情境若 query 不使用 instruction，可能下降約 1%～5%；多語情境建議 instruction 使用英文，因訓練時多數 instruction 原本是英文。

來源：

- Qwen 官方模型卡 Usage／Tip：<https://huggingface.co/Qwen/Qwen3-Embedding-4B#usage>
- Qwen 官方 GitHub Usage：<https://github.com/QwenLM/Qwen3-Embedding#usage>

### 官方 GGUF 與量化檔

Qwen 官方 GGUF model card 列出以下格式：`q4_K_M`、`q5_0`、`q5_K_M`、`q6_K`、`q8_0`、`f16`。官方 Hugging Face 檔案清單在查核當下顯示：

- Q4_K_M：2,496,703,776 bytes，約 2.33 GiB
- Q5_0：2,823,134,496 bytes，約 2.63 GiB
- Q5_K_M：2,888,936,736 bytes，約 2.69 GiB
- Q6_K：3,305,684,256 bytes，約 3.08 GiB
- Q8_0：4,279,660,224 bytes，約 3.99 GiB
- F16：8,049,889,824 bytes，約 7.50 GiB

來源：

- Qwen 官方 GGUF model card：<https://huggingface.co/Qwen/Qwen3-Embedding-4B-GGUF>
- Qwen 官方 GGUF 檔案頁：<https://huggingface.co/Qwen/Qwen3-Embedding-4B-GGUF/tree/main>
- Hugging Face 第一方 tree API（精確 bytes）：<https://huggingface.co/api/models/Qwen/Qwen3-Embedding-4B-GGUF/tree/main?recursive=true&expand=true>

### llama.cpp 官方推理路徑

Qwen 官方 GGUF 範例指定：

- 單次推理：`llama-embedding` 搭配 `--pooling last`；
- 本機服務：`llama-server` 搭配 `--embedding --pooling last -ub 8192`。

llama.cpp 官方 server 文件則確認：

- 支援 F16 與量化模型在 CPU／GPU 推理；
- `--embedding` 可把 server 限制為 embedding 用途；
- `--pooling` 支援 `none/mean/cls/last/rank`；
- 提供 OpenAI-compatible `POST /v1/embeddings`；
- 該路由要求 pooling 不能是 `none`，並會以 Euclidean norm 正規化輸出。

來源：

- Qwen 官方 GGUF llama.cpp 範例：<https://huggingface.co/Qwen/Qwen3-Embedding-4B-GGUF#llamacpp>
- llama.cpp 官方 server 文件：<https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md>
- llama.cpp `/v1/embeddings` 文件段落：<https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md#post-v1embeddings-openai-compatible-embeddings-api>

## 2. Gemini Embedding 001 官方規格

### 基本能力、token 與維度

- 模型代號：`gemini-embedding-001`。
- 輸入／輸出：文字輸入、文字 embedding 輸出。
- 輸入 token 上限：2,048。
- 預設輸出：3072 維。
- 可調整範圍：128～3072 維。
- Google 推薦維度：768、1536 或 3072。
- 使用 MRL，因此可取較低維度，以換取較少儲存與下游計算。

來源：

- Google Gemini Embeddings guide，Model versions／Controlling embedding size：<https://ai.google.dev/gemini-api/docs/embeddings#model-versions>
- Google Gemini Embeddings guide：<https://ai.google.dev/gemini-api/docs/embeddings#control-embedding-size>

### 支援的 task type

`gemini-embedding-001` 可以在 `embedContent` 的 `task_type` 指定用途。官方列出的 task type 是：

- `SEMANTIC_SIMILARITY`：語意相似度；
- `CLASSIFICATION`：分類；
- `CLUSTERING`：分群；
- `RETRIEVAL_DOCUMENT`：建立要被搜尋的文件向量；
- `RETRIEVAL_QUERY`：一般檢索 query；
- `CODE_RETRIEVAL_QUERY`：用自然語言搜尋程式碼；文件／code block 端仍用 `RETRIEVAL_DOCUMENT`；
- `QUESTION_ANSWERING`：問答問題端；文件端用 `RETRIEVAL_DOCUMENT`；
- `FACT_VERIFICATION`：事實核對目標文字端；證據文件端用 `RETRIEVAL_DOCUMENT`。

對本案的一般知識檢索，Gemini 應固定為：文件端 `RETRIEVAL_DOCUMENT`、問題端 `RETRIEVAL_QUERY`。若現行系統實際使用其他 task type，測試報告必須如實記錄，不能悄悄換成更有利的新設定。

來源：

- Google Supported task types：<https://ai.google.dev/gemini-api/docs/embeddings#supported-task-types>
- Google Embeddings API `TaskType` enum：<https://ai.google.dev/api/embeddings#tasktype>

### 正規化要求

- 3072 維輸出已正規化。
- `gemini-embedding-001` 若要求低於 3072 維，Google 明確要求自行做 normalization。
- 因此本案若比較 768 或 1536 維，Gemini 端必須先做 L2 normalization；Qwen 端也必須保持相同的 L2 normalization，才能公平使用 cosine／dot-product 比較。

來源：

- Google Ensuring quality for smaller dimensions：<https://ai.google.dev/gemini-api/docs/embeddings#quality-for-smaller-dimensions>
- Google Migration from `gemini-embedding-001`，Normalization：<https://ai.google.dev/gemini-api/docs/embeddings#migration>

### 服務終止資訊

Google Deprecations 頁目前列出：

- `gemini-embedding-001` 發布日：2025-07-14；
- shutdown date：2028-05-14；
- 建議替代：`gemini-embedding-2`。

同一頁也說明，表格中的 shutdown date 代表「最早可能退役日期」，確切日期會提前通知；因此文件中應寫「目前列定 2028-05-14／最早可能日期」，不要誤寫成永不變動的保證日期。

來源：

- Google Gemini API Deprecations，Embedding models：<https://ai.google.dev/gemini-api/docs/deprecations#embedding-models>

## 3. 公平比較必須控制的項目

以下是依上述官方規格推導出的實驗設計，不是廠商宣稱的勝負結果。

### A. 固定資料與正確答案

- 同一份 frozen corpus、完全相同的原始文字、切塊、標題、metadata 與過濾條件。
- 同一批真實 query，並預先建立人工 relevance labels／預期答案；評分人不看模型名稱。
- 每個 query 同時跑兩邊，不因模型而改寫問題。
- 測試文字需控制在 Gemini 的 2,048-token 上限內；任何截斷、拒絕或空向量都要記錄。由於兩者 tokenizer 不同，報告同時保存原文字元數與各自 token 數。

### B. 固定模型正確用法

- Gemini 文件端：`RETRIEVAL_DOCUMENT`；問題端：`RETRIEVAL_QUERY`。
- Qwen 文件端：不加 instruction；問題端：固定同一個英文 retrieval instruction，例如官方範例的 `Given a web search query, retrieve relevant passages that answer the query`。
- Qwen 固定 last-token pooling。
- 兩邊輸出都做 L2 normalization；搜尋一律使用相同 cosine similarity（或 normalized dot product）。
- 不加 reranker、LLM 改寫、hybrid keyword boost 或不同 metadata filter；這些若要評估，另開第二階段。

### C. 維度與索引分離

- 主比較建議先用共同且 Gemini 官方推薦的 768 維，控制儲存與檢索計算量。
- 再做「各自品質上限」副比較：Gemini 3072 維、Qwen 2560 維；這組不是同維度公平賽，只用來看各模型最佳可用品質。
- 如需判斷 Qwen 的維度折衷，可另測 1024／2560；不得拿不同維度的結果當作純模型差異。
- 不可把 Qwen query 放進 Gemini 向量表，或反向混用。Google 已明示即使自家 `gemini-embedding-001` 與 `gemini-embedding-2` 之間空間也不相容、升級需全部重嵌；跨 Qwen／Gemini 更應建立獨立 index/table，以同一份 query 各查各的索引。

來源（空間不相容與重嵌要求）：<https://ai.google.dev/gemini-api/docs/embeddings#migration>

### D. 固定搜尋器與效能環境

- 品質主測優先用 exact search；若使用 LanceDB ANN，兩邊固定相同 index 類型、distance、top-k、refine／probe 參數，並另測 ANN recall，避免把索引近似誤差算成模型差異。
- 報告至少列 Hit@1／Hit@5、MRR、nDCG@5、無正確答案率與代表性錯誤案例；平均分數之外保留繁中、英文、跨語言、短問句、長問句等切片。
- 效能測試與品質測試分開。Qwen 記錄 cold start、warm latency、chunks/sec、峰值記憶體、模型量化；Gemini 記錄端到端 API latency、錯誤／限流與批次設定。
- Qwen Q4_K_M 與 Q5_K_M 分別跑，不把不同量化混成一個「Qwen」結果；模型 revision、GGUF SHA-256、llama.cpp revision 與所有 runtime 參數必須鎖定。

## 4. 官方資料可確認的限制與未知數

### Qwen

- 官方公開 benchmark 表格中的 Gemini 對手是 `gemini-embedding-exp-03-07`，不是 `gemini-embedding-001`；不能直接回答本案誰較準。
- 官方 GGUF card 列出了量化檔，但 Evaluation 表格未分別公布 Q4_K_M、Q5_K_M 等量化後的品質衰退，也未提供本案 Apple Silicon 的實際速度／記憶體數字；必須實機測。
- query instruction 是品質設定的一部分，且官方建議英文。未固定 instruction，就無法重現結果。
- 官方 Transformers 範例要求 `transformers>=4.51.0`；更舊版本可能出現 `KeyError: 'qwen3'`。
- 官方 llama.cpp 範例指定 last pooling。若整合層誤用 mean／cls，結果不是官方建議路徑。
- 官方雖宣告 32～2560 維 MRL，但 GGUF 的 llama.cpp 範例沒有展示低維輸出的具體裁切參數；整合時需驗證 adapter 是否取正確前綴並重新正規化，不能只假設 API 回傳指定維度。

來源：

- Qwen 官方模型卡：<https://huggingface.co/Qwen/Qwen3-Embedding-4B>
- Qwen 官方 GGUF model card：<https://huggingface.co/Qwen/Qwen3-Embedding-4B-GGUF>

### Gemini Embedding 001

- 僅支援文字，輸入上限 2,048 tokens；比 Qwen 官方 32K 上限小。
- 低於 3072 維時不會自動完成所需 normalization，必須由客戶端處理。
- task type 會改變 embedding 用途；query／document 設錯或混用會讓比較失真。
- `gemini-embedding-001` 與後繼 `gemini-embedding-2` 的向量空間不相容，官方要求遷移時全部重嵌；因此依賴雲端模型仍需規劃重建索引。
- Google 目前列出 2028-05-14 為 shutdown date，但頁面總則把表列日期定義為最早可能退役日期，確切日仍以後續通知為準。

來源：

- Google Gemini Embeddings guide：<https://ai.google.dev/gemini-api/docs/embeddings>
- Google Gemini API Deprecations：<https://ai.google.dev/gemini-api/docs/deprecations#embedding-models>

## 5. 給 PoC 的最小可重現設定

### 主品質比較

- Corpus／chunks／queries／labels：完全相同且凍結。
- 維度：768。
- Gemini：`gemini-embedding-001`；document=`RETRIEVAL_DOCUMENT`，query=`RETRIEVAL_QUERY`；768 維後做 L2 normalization。
- Qwen：官方 Q4_K_M 與 Q5_K_M 各一組；query 使用固定英文 retrieval instruction、document 無 instruction；last-token pooling；輸出取 768 維並確認 L2 normalization。
- 檢索：獨立向量表、cosine、exact top-k；不加 reranker／hybrid／query rewrite。
- 評分：Hit@1、Hit@5、MRR、nDCG@5，加上繁中與跨語言切片。

### 次要比較

- Qwen 2560 vs Gemini 3072：各自原生／最大維度的品質上限。
- Qwen Q4_K_M vs Q5_K_M：品質、速度、峰值記憶體三者折衷。
- Cold vs warm：首次啟動與常態查詢分開報告。

## 6. 不能從官方資料直接下的結論

- 不能說 Qwen 一定比 `gemini-embedding-001` 準或一定較差。
- 不能用 Qwen card 對 `gemini-embedding-exp-03-07` 的排行榜，替代本案對 `gemini-embedding-001` 的實測。
- 不能只因兩邊都輸出 768 維就共用索引。
- 不能在沒有固定 instruction、task type、normalization、distance 與切塊的情況下解讀勝負。
- 不能由模型檔大小推定實際峰值 RAM 或每秒 chunks；這些必須在目標電腦實測。
