# meta-controller

Blueprint から製品側の `.factory-config/base/` をコンパイルし、以後は差分を検出して Reconcile する。
抽象は 2 号機との差分から抽出するので、P6 まで実装しない。P0〜P5 の base は手で置く。
