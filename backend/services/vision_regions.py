"""Bounded image subdivision on output truncation, preserving crop locations."""
from io import BytesIO
from backend.services.vision import normalize_vision_result, VisionResult


def analyze_regions(call, data, media_type, location, depth=0, box=None):
    try:
        return call(data, media_type, location)
    except Exception as exc:
        if getattr(exc, 'code', '') != 'vision_output_truncated' or depth >= 2:
            raise
        from PIL import Image
        with Image.open(BytesIO(data)) as image:
            width, height = image.size
            # Overlap protects cut lines. All regions must succeed.
            if width > height:
                boxes = [(0, 0, width // 2 + 40, height), (max(0, width // 2 - 40), 0, width, height)]
            else:
                boxes = [(0, 0, width, height // 2 + 40), (0, max(0, height // 2 - 40), width, height)]
            parts = []
            for i, bounds in enumerate(boxes):
                stream = BytesIO()
                image.crop(bounds).save(stream, format='PNG')
                part = normalize_vision_result(analyze_regions(call, stream.getvalue(), 'image/png', f'{location}/crop:{i}', depth + 1, bounds))
                part.source_refs.append({'location': f'{location}/crop:{i}', 'bbox': list(bounds), 'coordinate_frame': location})
                parts.append(part)
        fields = {}
        for part in parts:
            for key, value in part.fields.items():
                if isinstance(value, list):
                    fields.setdefault(key, []).extend(value)
                elif isinstance(value, dict) and isinstance(value.get('value'), list):
                    fields.setdefault(key, {**value, 'value': []})['value'].extend(value['value'])
                elif key not in fields:
                    fields[key] = value
        return VisionResult(text='\n'.join(p.text for p in parts), fields=fields,
                            source_refs=[r for p in parts for r in p.source_refs],
                            warnings=[w for p in parts for w in p.warnings], provider=parts[0].provider)
