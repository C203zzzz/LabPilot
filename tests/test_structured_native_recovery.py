import fitz

from backend.services.structured_pdf import complete_native_regions


def test_picture_annotations_are_recovered_without_duplicate_footer(tmp_path):
    path = tmp_path / 'brochure.pdf'
    with fitz.open() as pdf:
        page = pdf.new_page()
        page.insert_text((40, 60), 'LED illumination 7 W; working distance 85 mm')
        page.insert_text((40, 700), 'Contact manufacturer for further information')
        pdf.save(path)
    original = [{'id': 'footer', 'page': 1, 'label': 'text',
                 'text': 'Contact manufacturer for further information'}]
    result = complete_native_regions(path, original, [1])
    assert len(result) == 2
    recovered = result[1]
    assert '7 W' in recovered['text'] and '85 mm' in recovered['text']
    assert recovered['parser'] == 'pymupdf_native'
    assert recovered['bbox']['coord_origin'] == 'TOPLEFT'
    assert len(original) == 1
    assert complete_native_regions(path, result, [1]) == result


def test_native_recovery_does_not_flatten_existing_table_or_read_unselected_page(tmp_path):
    path = tmp_path / 'table.pdf'
    with fitz.open() as pdf:
        page = pdf.new_page()
        page.insert_text((40, 60), 'A100 A200 Volume 10 20')
        pdf.new_page().insert_text((40, 60), 'Unselected model B300')
        pdf.save(path)
    regions = [{'page': 1, 'label': 'table', 'text': '| A100 | A200 |\n| 10 | 20 |',
                'bbox': {'l': 20, 't': 20, 'r': 300, 'b': 90, 'coord_origin': 'TOPLEFT'}}]
    assert complete_native_regions(path, regions, [1]) == regions
