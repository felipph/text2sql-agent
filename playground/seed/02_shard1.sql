-- Seed playground: shard1

CREATE TABLE IF NOT EXISTS recebiveis_123 (
  cnpj VARCHAR(14) NOT NULL,
  valor NUMERIC(14,2) NOT NULL,
  data_vencimento DATE NOT NULL,
  status VARCHAR(20) NOT NULL
);

TRUNCATE TABLE recebiveis_123;

INSERT INTO recebiveis_123 (cnpj, valor, data_vencimento, status) VALUES ('12345678000190', 100.00, '2026-01-15', 'pago');
INSERT INTO recebiveis_123 (cnpj, valor, data_vencimento, status) VALUES ('12345678000190', 50.00, '2026-02-01', 'pendente');
INSERT INTO recebiveis_123 (cnpj, valor, data_vencimento, status) VALUES ('12345678000190', 25.00, '2026-02-15', 'pago');
