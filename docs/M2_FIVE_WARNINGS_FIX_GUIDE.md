# M2 — Rà soát 5 nhóm WARN còn lại (GBao)

## Trạng thái

Bản này **không** tuyên bố sửa xong 5 loại WARN. Dữ liệu log GBao gồm 724 gathering, 540 structural leaves, 90 citation, 6 empty TeX trees, 4 encoding, chưa có corpus nguồn để xác minh nguyên nhân cụ thể của từng bài trong môi trường tạo patch.

Thay đổi code có thể kiểm chứng:

1. `validate_m2.py` dùng cùng bộ nhận diện `iter_cite_groups` với Member 3; nhận `\Cite` và biblatex multicites, không coi `\nocite{*}` là key thiếu.
2. `diagnose_m2_warnings.py` đọc trực tiếp `corpus`, `parsed`, `.bib`/`.bbl` và `gathering.json`, xuất chứng cứ và phân loại có điều kiện. **Không chỉnh dữ liệu, không tự loại WARN**.
3. `test_m2_case_diagnostics.py` có hồi quy cho cả năm nhóm và validator citation.

Các lỗi nguồn thật (mất bib, mất TeX body, file include thiếu, ký tự mất khi chuyển đổi) chỉ sửa khi truy được ví dụ nguyên bản, không thay bằng đoán định.

## Chạy trên Colab đã có `WORK_DATA` (sau bước full)

Sau khi checkout mã mới vào `REPO_DIR` (xem phần Git), chạy:

```python
import subprocess, sys
subprocess.run([sys.executable, "src/validate_m2.py", "--data-dir", str(WORK_DATA),
                "--require-parquet"], cwd=REPO_DIR, check=True)
subprocess.run([sys.executable, "src/diagnose_m2_warnings.py", "--data-dir", str(WORK_DATA)],
               cwd=REPO_DIR, check=True)
```

Hai file được tạo: `logs/warning_evidence_m2.csv`, `logs/warning_evidence_summary_m2.json`.
Chỉ chạy riêng các bài có ưu tiên cao (ví dụ):

```bash
python src/diagnose_m2_warnings.py --data-dir /content/gbao_m2_full_work \
  --paper-ids 2604.07041 2604.08146 2604.08386 2604.07072 2604.07242 2604.06594
```

Không cần chạy lại parser của 5.000 bài chỉ vì **công cụ chẩn đoán** và việc chỉnh cú pháp validator. Nếu sau đó sửa parser thật, phải chạy lại các bài liên quan với `--force`; benchmark full phải tương ứng đúng commit cuối. **Không chạy `pipeline_m2.py --ids ...` trên bộ dữ liệu full mà thiếu `--no-export`: việc export chỉ các ID được chọn có thể thay thế bảng Parquet full.** Chạy test trên bản sao corpus riêng, hoặc thêm `--no-export` và chỉ export lại sau khi đủ dữ liệu.

## Quyết định sửa theo bằng chứng

| Check | Kết quả cần xem trong evidence | Quyết định |
|---|---|---|
| `empty_tex_tree` | `content_present_but_no_hierarchy_edges` | **Lỗi nghiêm trọng**: kiểm tra root, `gatherer`, `cleaner`, `normalizer`, `hierarchy.build`; viết test tái hiện trước khi sửa. `empty_compiled_body` có thể là nguồn rỗng, không tự sinh node giả |
| `citation_key_coverage` | `source_defined_missing_from_output` / `no_exported_bibliography` / `citation_case_or_alias_mismatch` | Nếu key có trong `.bib/.bbl` nhưng mất khỏi `bibtex.json`, kiểm tra `gatherer.bib_files`, `bibitem_parser.parse`, `dedup_bibtex`; xử lý nguyên nhân. Chỉ đổi case khi key canonical duy nhất; không tự tạo entry |
| `gathering_problems` | `multiple_root_candidates` / `missing_source_dependency` | Nhiều root hợp lệ là thông tin, không sửa thuật toán chỉ để tắt WARN. Thiếu `\input` cần đối chiếu file thật và logic `targets`/include; root chọn sai cần test từ nguyên bản |
| `nonstandard_leaf_kinds` | Ví dụ node `heading`, `item`, `paragraph`, số con | Structural node không có con có thể hợp lệ. Không tự xóa node hoặc thêm câu giả. Nếu nội dung đáng lẽ ở dưới node bị rơi mất, sửa `hierarchy._Builder.flush`/`leaf_extractor` dựa vào source và test |
| `encoding_replacement_chars` | Vị trí `U+FFFD` và `source_files_containing_U+FFFD` | Nếu nguồn đã có `�`, không thể khôi phục ký tự nguyên bản chỉ từ output. Nếu nguồn sạch mà output bị `�`, kiểm tra đường đi `gatherer.read_text` → converter/cleaner và encoding; thử trên file nguyên bản |

Các field `source_defined_missing_from_output` là dấu hiệu cần kiểm tra; một entry được khai báo trong source nhưng không được export còn có thể do nó không được trích dẫn trong phiên bản được chọn. Đối chiếu `cited_keys` và nguồn thực tế.

## Các bước trên Windows/VS Code (`m2/tram_fix`)

**Không áp dụng hai bản patch lên cùng một source.**

1. `git branch --show-current`; xác nhận `m2/tram_fix`.
2. `git status --short`, `git stash list`; nếu tree đang sạch trên commit nền `03c8a6b`, dùng `M2_FIVE_WARNINGS_FULL_FROM_MAIN.patch` (**toàn bộ sửa M2 + công cụ mới**).
3. Nếu tree đang có đúng các thay đổi từ `M2_GBAO_ALL_FIXES_AND_AUDIT.patch` (chưa commit), dùng `M2_FIVE_WARNINGS_INCREMENTAL.patch` (**chỉ các thay đổi mới**).
4. Nếu HEAD đã commit các thay đổi trước, cũng dùng incremental khi `git apply --check` thành công. Không `stash pop` để chồng code cũ.
5. Kiểm tra `git apply --check <patch>` trước `git apply <patch>`. Nếu lỗi, dừng và so sánh diff; không dùng `--force`.
6. Chạy `.\.venv\Scripts\python.exe -m unittest discover -s src -p "test*.py" -v`.
7. Chạy lại validator và diagnostic từ corpus đang có; không upload `data/` hay các Parquet lên GitHub.
8. `git diff --check`, `git status --short`, `git add -- README.md docs src`; xem `git diff --cached --name-status`.
9. Commit và `git push -u origin m2/tram_fix`; tạo PR **base `m2/main`**, compare `m2/tram_fix`. Chỉ merge vào `main` sau review của nhóm.

## Điểm chưa xác minh

- Chưa có corpus TeX/JSON thật của 6 cây rỗng hoặc 90 bài citation WARN trong môi trường tạo patch.
- Chưa chạy lại 5.000 bài sau chỉnh cú pháp validator.
- Chưa thể chứng minh 724 gathering WARN đều vô hại; mỗi `problems` cần phân nhóm.
- Không được diễn giải `0 FAIL` là không còn vấn đề về nội dung.
