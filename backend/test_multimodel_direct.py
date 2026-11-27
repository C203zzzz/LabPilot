#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""直接测试多型号宽表功能"""

import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

# 测试数据
MULTIMODEL_TABLE = """
型号              H1650        H1650R       H2050R
最高转速(rpm)     16500        16500        20500
最大相对离心力(×g)    23950        23950        36900
浴槽容积(L)       3.5          3.5          11.5
控温范围(℃)      -             -20~40       -20~40
噪音(dB)          ≤65          ≤65          ≤68
"""

REQUIREMENTS = [
    {'name': '最高转速', 'value': 15000, 'unit': 'rpm', 'direction': 'min'},
    {'name': '最大相对离心力', 'value': 20000, 'unit': '×g', 'direction': 'min'},
    {'name': '浴槽容积', 'value': 3.0, 'unit': 'L', 'direction': 'min'},
]

def test_multimodel_extraction():
    """直接测试多型号宽表提取函数"""
    print("=" * 60)
    print("直接测试多型号宽表提取函数")
    print("=" * 60)

    try:
        from backend.services.research_planning import _extract_from_multimodel_table

        detail_lines = MULTIMODEL_TABLE.splitlines()
        target_model = 'H1650R'

        print(f"\n目标型号: {target_model}")
        print(f"表格行数: {len(detail_lines)}")
        print(f"要求数量: {len(REQUIREMENTS)}")

        results = _extract_from_multimodel_table(detail_lines, target_model, REQUIREMENTS)

        print(f"\n提取结果数量: {len(results)}")
        for r in results:
            print(f"  - {r['name']}: {r['value']} {r['unit']}")
            print(f"    证据: {r['evidence'][:60]}...")

        if len(results) > 0:
            print("\n[PASS] 多型号宽表提取功能正常")
            return True
        else:
            print("\n[FAIL] 多型号宽表提取失败：未提取到任何参数")
            return False

    except Exception as e:
        print(f"\n[ERROR] 测试失败: {e}")
        import traceback
        traceback.print_exc()
        return False

if __name__ == '__main__':
    success = test_multimodel_extraction()
    sys.exit(0 if success else 1)
