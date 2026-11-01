CREATE TABLE IF NOT EXISTS products (
    id SERIAL PRIMARY KEY,
    sku VARCHAR(64) NOT NULL,
    name VARCHAR(120) NOT NULL,
    category VARCHAR(50) NOT NULL,
    price NUMERIC(10, 2) NOT NULL,
    stock INT NOT NULL,
    specs JSONB NOT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_products_category ON products(category);

-- Generate 50,000 distinct records for realistic stress testing
INSERT INTO products (sku, name, category, price, stock, specs)
SELECT 
    UPPER(SUBSTRING(MD5(i::text) FROM 1 FOR 12)) AS sku,
    'Enterprise_Component_' || i AS name,
    CASE (i % 8)
        WHEN 0 THEN 'Processors'
        WHEN 1 THEN 'Networking_Switches'
        WHEN 2 THEN 'NVMe_Storage'
        WHEN 3 THEN 'Server_Chassis'
        WHEN 4 THEN 'Memory_Modules'
        WHEN 5 THEN 'Optical_Transceivers'
        WHEN 6 THEN 'Power_Supplies'
        ELSE 'Security_Appliances'
    END AS category,
    ROUND((RANDOM() * 1950 + 50)::numeric, 2) AS price,
    FLOOR(RANDOM() * 500 + 1)::int AS stock,
    jsonb_build_object(
        'batch_id', (i % 250) + 1,
        'warranty_years', (i % 5) + 1,
        'rack_units', (i % 4) + 1,
        'certified', (i % 2 = 0)
    ) AS specs
FROM generate_series(1, 50000) AS s(i);
