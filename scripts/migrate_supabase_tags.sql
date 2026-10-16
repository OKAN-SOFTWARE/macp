-- area→tags 마이그레이션 (PR #19 / todo-area-to-tags-refactor)
-- Supabase SQL 에디터에서 실행. PostgREST 서비스키로는 DDL이 안 되므로 이 한 블록은 대시보드에서 수동 실행 필요.
--
-- 1) tags 컬럼 추가 (jsonb 배열, 기본 빈 배열). 이미 있으면 무시.
ALTER TABLE tasks
    ADD COLUMN IF NOT EXISTS tags jsonb NOT NULL DEFAULT '[]'::jsonb;

-- 2) area 컬럼은 롤백 대비로 즉시 DROP하지 않는다. (orphan으로 남겨두고, 컷오버 안정화 후 별도 정리)
--    backfill은 scripts/migrate_supabase_tags.py 로 실행 (body frontmatter tags 파싱 + [area] 보강).
--    (SQL만으로 간단 백필하려면 아래 주석 해제 — area를 단일 태그로 넣음)
-- UPDATE tasks
--     SET tags = to_jsonb(ARRAY[area])
--     WHERE area IS NOT NULL AND tags = '[]'::jsonb;
