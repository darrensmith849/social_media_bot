#database_setup.sql

-- 1. The Clients Table (The "Master" Record)
CREATE TABLE IF NOT EXISTS clients (
    id VARCHAR(50) PRIMARY KEY, -- e.g., 'client_123' or 'joes_burgers'
    name VARCHAR(255) NOT NULL,
    website VARCHAR(255),
    industry VARCHAR(100),
    city VARCHAR(100),
    
    -- This JSON column stores the "Brand DNA" (Tone, Constraints, Tips, etc.)
    attributes JSON, 
    
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 2. The Posts Table (History of what we sent)
CREATE TABLE IF NOT EXISTS published_posts (
    id INT AUTO_INCREMENT PRIMARY KEY,
    client_id VARCHAR(50) NOT NULL,
    platform VARCHAR(50) NOT NULL, -- 'x', 'linkedin', 'facebook'
    template_key VARCHAR(100),
    text_hash VARCHAR(64),         -- To prevent duplicates
    external_id VARCHAR(255),      -- The ID returned by X/LinkedIn
    posted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    
    FOREIGN KEY (client_id) REFERENCES clients(id) ON DELETE CASCADE
);

-- 3. The View (This is what main.py reads)
-- It maps our table columns to the names the Bot expects.
CREATE OR REPLACE VIEW bot_clients_v AS
SELECT 
    id,
    name,
    industry,
    city,
    attributes
FROM clients;
