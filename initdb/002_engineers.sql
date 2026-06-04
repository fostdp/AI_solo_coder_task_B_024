ALTER TABLE engineers ADD COLUMN IF NOT EXISTS factory_id VARCHAR(32);

INSERT INTO factories (factory_id, factory_name, address, lat, lng, status) VALUES
('F-SH', '上海晶圆厂', '上海市浦东新区张江高科技园区', 31.2086, 121.5896, 'active'),
('F-SZ', '深圳晶圆厂', '深圳市南山区科技园', 22.5431, 113.9413, 'active'),
('F-BJ', '北京晶圆厂', '北京市亦庄经济技术开发区', 39.7817, 116.5047, 'active')
ON CONFLICT (factory_id) DO NOTHING;

INSERT INTO engineers (engineer_id, name, specialty, factory_id) VALUES
('ENG-001', '张伟', 'CVD', 'F-SH'),   ('ENG-002', '李强', 'PVD', 'F-SH'),
('ENG-003', '王芳', 'ETCH', 'F-SH'),  ('ENG-004', '刘洋', 'LITHO', 'F-SH'),
('ENG-005', '陈磊', 'IMPLANT', 'F-SH'),('ENG-006', '赵敏', 'CMP', 'F-SH'),
('ENG-007', '周涛', 'DIFF', 'F-SZ'),  ('ENG-008', '吴鹏', 'CLEAN', 'F-SZ'),
('ENG-009', '郑华', 'METRO', 'F-SZ'), ('ENG-010', '孙丽', 'DEPO', 'F-SZ'),
('ENG-011', '马超', 'CVD', 'F-SZ'),   ('ENG-012', '朱军', 'PVD', 'F-SZ'),
('ENG-013', '胡明', 'ETCH', 'F-BJ'),  ('ENG-014', '林峰', 'LITHO', 'F-BJ'),
('ENG-015', '何勇', 'IMPLANT', 'F-BJ'),('ENG-016', '高健', 'CMP', 'F-BJ'),
('ENG-017', '罗斌', 'DIFF', 'F-BJ'),  ('ENG-018', '谢刚', 'CLEAN', 'F-BJ'),
('ENG-019', '韩雪', 'METRO', 'F-BJ'), ('ENG-020', '唐杰', 'DEPO', 'F-BJ')
ON CONFLICT (engineer_id) DO NOTHING;

INSERT INTO spare_parts (part_id, part_name, equipment_type, stock_quantity, safe_stock_level, unit_price, supplier) VALUES
('SP-CVD-001', 'CVD反应室密封圈', 'CVD', 15, 10, 2500.00, 'Applied Materials'),
('SP-CVD-002', 'CVD加热组件', 'CVD', 5, 8, 15000.00, 'Applied Materials'),
('SP-PVD-001', 'PVD靶材组件', 'PVD', 8, 6, 8000.00, 'Lam Research'),
('SP-PVD-002', 'PVD磁控管', 'PVD', 4, 5, 12000.00, 'Lam Research'),
('SP-ETCH-001', 'ETCH电极组件', 'ETCH', 6, 5, 18000.00, 'Tokyo Electron'),
('SP-ETCH-002', 'ETCH气体喷头', 'ETCH', 10, 8, 3500.00, 'Tokyo Electron'),
('SP-LITHO-001', 'LITHO投影镜头', 'LITHO', 2, 3, 250000.00, 'ASML'),
('SP-LITHO-002', 'LITHO光源模块', 'LITHO', 3, 2, 80000.00, 'ASML'),
('SP-IMPLANT-001', 'IMPLANT离子源', 'IMPLANT', 4, 4, 35000.00, 'Varian'),
('SP-IMPLANT-002', 'IMPLANT晶圆夹具', 'IMPLANT', 8, 6, 5000.00, 'Varian'),
('SP-CMP-001', 'CMP抛光垫', 'CMP', 20, 15, 1200.00, 'Cabot Micro'),
('SP-CMP-002', 'CMP修整器', 'CMP', 10, 8, 6000.00, 'Cabot Micro'),
('SP-DIFF-001', 'DIFF石英管', 'DIFF', 3, 4, 12000.00, 'Tokyo Electron'),
('SP-DIFF-002', 'DIFF热电偶组', 'DIFF', 8, 6, 800.00, 'Tokyo Electron'),
('SP-CLEAN-001', 'CLEAN超声波振子', 'CLEAN', 12, 10, 2000.00, 'Screen Holdings'),
('SP-CLEAN-002', 'CLEAN化学喷嘴', 'CLEAN', 15, 10, 500.00, 'Screen Holdings'),
('SP-METRO-001', 'METRO光学传感器', 'METRO', 8, 6, 15000.00, 'KLA-Tencor'),
('SP-METRO-002', 'METRO探针卡', 'METRO', 10, 8, 3000.00, 'KLA-Tencor'),
('SP-DEPO-001', 'DEPO前驱体气瓶', 'DEPO', 6, 5, 8000.00, 'Applied Materials'),
('SP-DEPO-002', 'DEPO衬底加热器', 'DEPO', 4, 4, 22000.00, 'Applied Materials')
ON CONFLICT (part_id) DO NOTHING;
