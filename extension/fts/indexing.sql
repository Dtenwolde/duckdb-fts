DROP SCHEMA IF EXISTS %fts_schema% CASCADE;
CREATE SCHEMA %fts_schema%;

CREATE MACRO %fts_schema%.tokenize(s) AS stem(unnest(string_split_regex(regexp_replace(lower(strip_accents(s)), '[^a-z]', ' ', 'g'), '\s+')), '%stemmer%');

-- Sequences for stable, incrementable IDs
CREATE SEQUENCE %fts_schema%.docid_seq;
CREATE SEQUENCE %fts_schema%.termid_seq;

CREATE TABLE %fts_schema%.docs AS (
    SELECT
        nextval('%fts_schema%.docid_seq') AS docid,
        %input_id% AS name,
        0 AS len
    FROM
        %input_schema%.%input_table%
);

CREATE TABLE %fts_schema%.terms AS (
    SELECT
        term,
        docid,
        row_number() OVER (PARTITION BY docid) AS pos
    FROM (
        SELECT
            %fts_schema%.tokenize(%input_val%) AS term,
            row_number() OVER (PARTITION BY (SELECT NULL)) AS docid
        FROM %input_schema%.%input_table%
    ) AS sq
    WHERE
        term != ''
);

UPDATE %fts_schema%.docs d
SET len = (
    SELECT count(term)
    FROM %fts_schema%.terms t
    WHERE t.docid = d.docid
);

CREATE TABLE %fts_schema%.dict AS
WITH distinct_terms AS (
    SELECT DISTINCT term, docid
    FROM %fts_schema%.terms
    ORDER BY docid
)
SELECT
    nextval('%fts_schema%.termid_seq') AS termid,
    term,
    0 AS df
FROM
    distinct_terms;

ALTER TABLE %fts_schema%.terms ADD termid INT;
UPDATE %fts_schema%.terms t
SET termid = (
    SELECT termid
    FROM %fts_schema%.dict d
    WHERE t.term = d.term
);
ALTER TABLE %fts_schema%.terms DROP term;

UPDATE %fts_schema%.dict d
SET df = (
    SELECT count(distinct docid)
    FROM %fts_schema%.terms t
    WHERE d.termid = t.termid
    GROUP BY termid
);

ALTER TABLE %fts_schema%.dict ADD UNIQUE (term);

CREATE TABLE %fts_schema%.stats AS (
    SELECT COUNT(docs.docid) AS num_docs, SUM(docs.len) / COUNT(docs.len) AS avgdl
    FROM %fts_schema%.docs AS docs
);

-- Incremental trigger: fires after every INSERT on the source table,
-- updating docs, dict, terms, and stats to reflect the new rows.
CREATE TRIGGER %fts_schema%_insert_trigger
AFTER INSERT ON %input_schema%.%input_table%
REFERENCING NEW TABLE AS inserted_rows
FOR EACH STATEMENT
WITH
    -- 1. Assign new docids and insert into docs
    new_docs AS MATERIALIZED (
        INSERT INTO %fts_schema%.docs
        SELECT
            nextval('%fts_schema%.docid_seq') AS docid,
            %input_id% AS name,
            0 AS len
        FROM inserted_rows
        RETURNING docid, name
    ),
    -- 2. Tokenize the new rows, join back to get docid
    new_tokens AS MATERIALIZED (
        SELECT
            %fts_schema%.tokenize(%input_val%) AS term,
            d.docid
        FROM inserted_rows ir
        JOIN new_docs d ON d.name = ir.%input_id%
        WHERE term != ''
    ),
    -- 3. Upsert terms into dict: insert new terms, bump df for existing ones
    dict_upsert AS MATERIALIZED (
        INSERT INTO %fts_schema%.dict
        SELECT
            nextval('%fts_schema%.termid_seq') AS termid,
            term,
            count(DISTINCT docid) AS df
        FROM new_tokens
        GROUP BY term
        ON CONFLICT (term) DO UPDATE SET df = dict.df + excluded.df
    ),
    -- 4. Insert into terms table (docid, termid, pos); join dict_upsert to get termids
    --    and create a pipeline dependency so dict_upsert completes first.
    terms_insert AS MATERIALIZED (
        INSERT INTO %fts_schema%.terms
        SELECT
            du.termid,
            t.docid,
            row_number() OVER (PARTITION BY t.docid) AS pos
        FROM new_tokens t
        JOIN dict_upsert du ON du.term = t.term
    ),
    -- 5. Update doc lengths from new_tokens (avoids querying terms table mid-trigger)
    doc_len_update AS MATERIALIZED (
        UPDATE %fts_schema%.docs d
        SET len = (SELECT count(*) FROM new_tokens t WHERE t.docid = d.docid)
        WHERE d.docid IN (SELECT docid FROM new_docs)
    )
-- 6. Update stats
UPDATE %fts_schema%.stats
SET
    num_docs = num_docs + (SELECT count(*) FROM new_docs),
    avgdl    = (
        SELECT SUM(len) / COUNT(len)
        FROM %fts_schema%.docs
    );

CREATE MACRO %fts_schema%.match_bm25(docname, query_string, k=1.2, b=0.75, conjunctive=0) AS docname IN (
    WITH tokens AS
        (SELECT DISTINCT %fts_schema%.tokenize(query_string) AS t),
    qtermids AS
        (SELECT termid FROM %fts_schema%.dict AS dict, tokens WHERE dict.term = tokens.t),
    qterms AS
        (SELECT termid, docid FROM %fts_schema%.terms AS terms WHERE termid IN (SELECT qtermids.termid FROM qtermids)),
    subscores AS (
        SELECT
            docs.docid, len, term_tf.termid, tf, df,
            (log(((SELECT num_docs FROM %fts_schema%.stats) - df + 0.5) / (df + 0.5))* ((tf * (k + 1)/(tf + k * (1 - b + b * (len / (SELECT avgdl FROM %fts_schema%.stats))))))) AS subscore
        FROM
            (SELECT termid, docid, COUNT(*) AS tf FROM qterms GROUP BY docid, termid) AS term_tf
        JOIN
            (SELECT docid FROM qterms GROUP BY docid HAVING CASE WHEN conjunctive THEN COUNT(DISTINCT termid) = (SELECT COUNT(*) FROM tokens) ELSE 1 END) AS cdocs
        ON
            term_tf.docid = cdocs.docid
        JOIN
            %fts_schema%.docs AS docs
        ON
            term_tf.docid = docs.docid
        JOIN
            %fts_schema%.dict AS dict
        ON
            term_tf.termid = dict.termid
    )
    SELECT name
    FROM (SELECT docid, sum(subscore) AS score FROM subscores GROUP BY docid) AS scores
    JOIN %fts_schema%.docs AS docs
    ON scores.docid = docs.docid ORDER BY score DESC LIMIT 1000
);