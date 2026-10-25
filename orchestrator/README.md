# orchestrator

制御プレーン。ジョブキュー（Postgres の SKIP LOCKED）・アドバイザリロック・成果物メタデータ・予算の打ち切りを持つ。
耐久ワークフロー（Temporal 等）は薄いインターフェースの裏に置き、採否は ADR で判断する（P0 Week 3）。
