package is.hail.expr.ir

import is.hail.HailSuite
import is.hail.annotations.{BroadcastIndexedSeq, BroadcastRow}
import is.hail.expr.types._
import is.hail.expr.types.virtual._
import is.hail.methods.{ForceCountMatrixTable, ForceCountTable}
import is.hail.rvd.{RVD, RVDType}
import is.hail.utils._
import org.apache.spark.sql.Row
import org.testng.annotations.{DataProvider, Test}

import scala.collection.mutable

class PruneSuite extends HailSuite {
  @Test def testUnionType() {
    val base = TStruct(
      "a" -> TStruct(
        "aa" -> TInt32(),
        "ab" -> TStruct(
          "aaa" -> TString())),
      "b" -> TInt32(),
      "c" -> TArray(TStruct(
        "ca" -> TInt32())))

    assert(PruneDeadFields.unify(base, TStruct()) == TStruct())
    assert(PruneDeadFields.unify(base, TStruct("b" -> TInt32())) == TStruct("b" -> TInt32()))
    assert(PruneDeadFields.unify(base, TStruct("a" -> TStruct())) == TStruct("a" -> TStruct()))
    assert(PruneDeadFields.unify(base, TStruct("a" -> TStruct()),
      TStruct("b" -> TInt32())) == TStruct("a" -> TStruct(), "b" -> TInt32()))
    assert(PruneDeadFields.unify(base, TStruct("c" -> TArray(TStruct()))) == TStruct("c" -> TArray(TStruct())))
    assert(PruneDeadFields.unify(base, TStruct("a" -> TStruct("ab" -> TStruct())),
      TStruct("c" -> TArray(TStruct()))) == TStruct("a" -> TStruct("ab" -> TStruct()), "c" -> TArray(TStruct())))
  }

  def checkMemo(ir: BaseIR, requestedType: BaseType, expected: Array[BaseType]) {
    val irCopy = ir.deepCopy()
    assert(PruneDeadFields.isSupertype(requestedType, irCopy.typ),
      s"not supertype:\n  super: ${ requestedType.parsableString() }\n  sub:   ${ irCopy.typ.parsableString() }")
    val ms = PruneDeadFields.ComputeMutableState(Memo.empty[BaseType], mutable.HashMap.empty)
    irCopy match {
      case mir: MatrixIR => PruneDeadFields.memoizeMatrixIR(mir, requestedType.asInstanceOf[MatrixType], ms)
      case tir: TableIR => PruneDeadFields.memoizeTableIR(tir, requestedType.asInstanceOf[TableType], ms)
      case ir: IR => PruneDeadFields.memoizeValueIR(ir, requestedType.asInstanceOf[Type], ms)
    }
    irCopy.children.zipWithIndex.foreach { case (child, i) =>
      if (expected(i) != null && expected(i) != ms.requestedType.lookup(child)) {
        fatal(s"For base IR $ir\n  Child $i\n  Expected: ${ expected(i) }\n  Actual:   ${ ms.requestedType.lookup(child) }")
      }
    }
  }

  def checkRebuild[T <: BaseIR](
    ir: T,
    requestedType: BaseType,
    f: (T, T) => Boolean = (left: TableIR, right: TableIR) => left == right) {
    val irCopy = ir.deepCopy()
    val ms = PruneDeadFields.ComputeMutableState(Memo.empty[BaseType], mutable.HashMap.empty)
    val rebuilt = (irCopy match {
      case mir: MatrixIR =>
        PruneDeadFields.memoizeMatrixIR(mir, requestedType.asInstanceOf[MatrixType], ms)
        PruneDeadFields.rebuild(mir, ms.rebuildState)
      case tir: TableIR =>
        PruneDeadFields.memoizeTableIR(tir, requestedType.asInstanceOf[TableType], ms)
        PruneDeadFields.rebuild(tir, ms.rebuildState)
      case ir: IR =>
        PruneDeadFields.memoizeValueIR(ir, requestedType.asInstanceOf[Type], ms)
        PruneDeadFields.rebuildIR(ir, BindingEnv(Env.empty, Some(Env.empty), Some(Env.empty)), ms.rebuildState)
    }).asInstanceOf[T]
    if (!f(ir, rebuilt))
      fatal(s"IR did not rebuild the same:\n  Base:    $ir\n  Rebuilt: $rebuilt")
  }

  lazy val tab = TableLiteral(TableKeyBy(
    TableParallelize(
      Literal(
        TStruct(
          "rows" -> TArray(TStruct("1" -> TString(),
            "2" -> TArray(TStruct("2A" -> TInt32())),
            "3" -> TString(),
            "4" -> TStruct("A" -> TInt32(), "B" -> TArray(TStruct("i" -> TString()))),
            "5" -> TString())),
          "global" -> TStruct("g1" -> TInt32(), "g2" -> TInt32())),
        Row(FastIndexedSeq(Row("hi", FastIndexedSeq(Row(1)), "bye", Row(2, FastIndexedSeq(Row("bar"))), "foo")), Row(5, 10))),
      None),
    FastIndexedSeq("3"),
    false).execute(ctx), ctx)

  lazy val tr = TableRead(tab.typ, false, new TableReader {
    def apply(tr: TableRead, ctx: ExecuteContext): TableValue = ???

    def partitionCounts: Option[IndexedSeq[Long]] = ???

    def fullType: TableType = tab.typ
  })

  val mType = MatrixType(
    TStruct("g1" -> TInt32(), "g2" -> TFloat64()),
    FastIndexedSeq("ck"),
    TStruct("ck" -> TString(), "c2" -> TInt32(), "c3" -> TArray(TStruct("cc" -> TInt32()))),
    FastIndexedSeq("rk"),
    TStruct("rk" -> TInt32(), "r2" -> TStruct("x" -> TInt32()), "r3" -> TArray(TStruct("rr" -> TInt32()))),
    TStruct("e1" -> TFloat64(), "e2" -> TFloat64()))
  val mat = MatrixLiteral(mType,
    RVD.empty(sc, mType.canonicalTableType.canonicalRVDType),
    Row(1, 1.0),
    FastIndexedSeq(Row("1", 2, FastIndexedSeq(Row(3)))))

  val mr = MatrixRead(mat.typ, false, false, new MatrixReader {
    override def columnCount: Option[Int] = None

    def partitionCounts: Option[IndexedSeq[Long]] = None

    def fullMatrixType: MatrixType = mat.typ

    def lower(mr: MatrixRead): TableIR = ???
  })

  val emptyTableDep = TableType(TStruct(), FastIndexedSeq(), TStruct())

  def tableRefBoolean(tt: TableType, fields: String*): IR = {
    var let: IR = True()
    fields.foreach { f =>
      val split = f.split("\\.")
      var ir: IR = split(0) match {
        case "row" => Ref("row", tt.rowType)
        case "global" => Ref("global", tt.globalType)
      }

      split.tail.foreach { field =>
        ir = GetField(ir, field)
      }
      let = Let(genUID(), ir, let)
    }
    let
  }

  def tableRefStruct(tt: TableType, fields: String*): IR = {
    MakeStruct(tt.key.map(k => k -> GetField(Ref("row", tt.rowType), k)) ++ FastIndexedSeq("foo" -> tableRefBoolean(tt, fields: _*)))
  }

  def matrixRefBoolean(mt: MatrixType, fields: String*): IR = {
    var let: IR = True()
    fields.foreach { f =>
      val split = f.split("\\.")
      var ir: IR = split(0) match {
        case "va" => Ref("va", mt.rowType)
        case "sa" => Ref("sa", mt.colType)
        case "g" => Ref("g", mt.entryType)
        case "global" => Ref("global", mt.globalType)
      }

      split.tail.foreach { field =>
        ir = GetField(ir, field)
      }
      let = Let(genUID(), ir, let)
    }
    let
  }

  def matrixRefStruct(mt: MatrixType, fields: String*): IR = {
    MakeStruct(FastIndexedSeq("foo" -> matrixRefBoolean(mt, fields: _*)))
  }

  def subsetTable(tt: TableType, fields: String*): TableType = {
    val rowFields = new ArrayBuilder[TStruct]()
    val globalFields = new ArrayBuilder[TStruct]()
    var noKey = false
    fields.foreach { f =>
      val split = f.split("\\.")
      split(0) match {
        case "row" =>
          rowFields += PruneDeadFields.subsetType(tt.rowType, split, 1).asInstanceOf[TStruct]
        case "global" =>
          globalFields += PruneDeadFields.subsetType(tt.globalType, split, 1).asInstanceOf[TStruct]
        case "NO_KEY" =>
          noKey = true
      }
    }
    val k = if (noKey) FastIndexedSeq() else tt.key
    tt.copy(
      key = k,
      rowType = PruneDeadFields.unify(tt.rowType, Array(PruneDeadFields.selectKey(tt.rowType, k)) ++ rowFields.result(): _*),
      globalType = PruneDeadFields.unify(tt.globalType, globalFields.result(): _*)
    )
  }

  def subsetMatrixTable(mt: MatrixType, fields: String*): MatrixType = {
    val rowFields = new ArrayBuilder[TStruct]()
    val colFields = new ArrayBuilder[TStruct]()
    val entryFields = new ArrayBuilder[TStruct]()
    val globalFields = new ArrayBuilder[TStruct]()
    var noRowKey = false
    var noColKey = false
    fields.foreach { f =>
      val split = f.split("\\.")
      split(0) match {
        case "va" =>
          rowFields += PruneDeadFields.subsetType(mt.rowType, split, 1).asInstanceOf[TStruct]
        case "sa" =>
          colFields += PruneDeadFields.subsetType(mt.colType, split, 1).asInstanceOf[TStruct]
        case "g" =>
          entryFields += PruneDeadFields.subsetType(mt.entryType, split, 1).asInstanceOf[TStruct]
        case "global" =>
          globalFields += PruneDeadFields.subsetType(mt.globalType, split, 1).asInstanceOf[TStruct]
        case "NO_ROW_KEY" =>
          noRowKey = true
        case "NO_COL_KEY" =>
          noColKey = true
      }
    }
    val ck = if (noColKey) FastIndexedSeq() else mt.colKey
    val rk = if (noRowKey) FastIndexedSeq() else mt.rowKey
    MatrixType(
      rowKey = rk,
      colKey = ck,
      globalType = PruneDeadFields.unify(mt.globalType, globalFields.result(): _*),
      colType = PruneDeadFields.unify(mt.colType, Array(PruneDeadFields.selectKey(mt.colType, ck)) ++ colFields.result(): _*),
      rowType = PruneDeadFields.unify(mt.rowType, Array(PruneDeadFields.selectKey(mt.rowType, rk)) ++ rowFields.result(): _*),
      entryType = PruneDeadFields.unify(mt.entryType, entryFields.result(): _*))
  }

  def mangle(t: TableIR): TableIR = {
    TableRename(
      t,
      t.typ.rowType.fieldNames.map(x => x -> (x + "_")).toMap,
      t.typ.globalType.fieldNames.map(x => x -> (x + "_")).toMap
    )
  }

  @Test def testTableJoinMemo() {
    val tk1 = TableKeyBy(tab, Array("1"))
    val tk2 = mangle(TableKeyBy(tab, Array("3")))
    val tj = TableJoin(tk1, tk2, "inner", 1)
    checkMemo(tj,
      subsetTable(tj.typ, "row.1", "row.4", "row.1_"),
      Array(
        subsetTable(tk1.typ, "row.1", "row.4"),
        subsetTable(tk2.typ, "row.1_", "row.3_")
      )
    )

    val tk3 = TableKeyBy(tab, Array("1", "2"))
    val tk4 = mangle(TableKeyBy(tab, Array("1", "2")))

    val tj2 = TableJoin(tk3, tk4, "inner", 1)
    checkMemo(tj2,
      subsetTable(tj2.typ, "row.3_"),
      Array(
        subsetTable(tk3.typ, "row.1", "row.2"),
        subsetTable(tk4.typ, "row.1_", "row.2_", "row.3_")
      ))

    checkMemo(tj2,
      subsetTable(tj2.typ, "row.3_", "NO_KEY"),
      Array(
        TableType(globalType = TStruct(), key = Array("1"), rowType = TStruct("1" -> TString())),
        TableType(globalType = TStruct(), key = Array("1_"), rowType = TStruct("1_" -> TString(), "3_" -> TString()))
      ))
  }

  @Test def testTableLeftJoinRightDistinctMemo() {
    val tk1 = TableKeyBy(tab, Array("1"))
    val tk2 = TableKeyBy(tab, Array("3"))
    val tj = TableLeftJoinRightDistinct(tk1, tk2, "foo")
    checkMemo(tj,
      subsetTable(tj.typ, "row.1", "row.4", "row.foo"),
      Array(
        subsetTable(tk1.typ, "row.1", "row.4"),
        subsetTable(tk2.typ)
      )
    )
  }

  @Test def testTableIntervalJoinMemo() {
    val tk1 = TableKeyBy(tab, Array("1"))
    val tk2 = TableKeyBy(tab, Array("3"))
    val tj = TableIntervalJoin(tk1, tk2, "foo", product=false)
    checkMemo(tj,
      subsetTable(tj.typ, "row.1", "row.4", "row.foo"),
      Array(
        subsetTable(tk1.typ, "row.1", "row.4"),
        subsetTable(tk2.typ)
      )
    )
  }

  @Test def testTableMultiWayZipJoinMemo() {
    val tk1 = TableKeyBy(tab, Array("1"))
    val ts = Array(tk1, tk1, tk1)
    val tmwzj = TableMultiWayZipJoin(ts, "data", "gbls")
    checkMemo(tmwzj, subsetTable(tmwzj.typ, "row.data.2", "global.gbls.g1"), ts.map { t =>
      subsetTable(t.typ, "row.2", "global.g1")
    })
  }

  @Test def testTableExplodeMemo() {
    val te = TableExplode(tab, Array("2"))
    checkMemo(te, subsetTable(te.typ), Array(subsetTable(tab.typ, "row.2")))
  }

  @Test def testTableFilterMemo() {
    checkMemo(TableFilter(tab, tableRefBoolean(tab.typ, "row.2")),
      subsetTable(tab.typ, "row.3"),
      Array(subsetTable(tab.typ, "row.2", "row.3"), null))
    checkMemo(TableFilter(tab, False()),
      subsetTable(tab.typ, "row.1"),
      Array(subsetTable(tab.typ, "row.1"), TBoolean()))
  }

  @Test def testTableKeyByMemo() {
    val tk = TableKeyBy(tab, Array("1"))
    checkMemo(tk, subsetTable(tk.typ, "row.2"), Array(subsetTable(tab.typ, "row.1", "row.2", "NO_KEY")))

    val tk2 = TableKeyBy(tab, Array("3"), isSorted = true)
    checkMemo(tk2, subsetTable(tk2.typ, "row.2"), Array(subsetTable(tab.typ, "row.2")))

  }

  @Test def testTableMapRowsMemo() {
    val tmr = TableMapRows(tab, tableRefStruct(tab.typ, "row.1", "row.2"))
    checkMemo(tmr, subsetTable(tmr.typ, "row.foo"), Array(subsetTable(tab.typ, "row.1", "row.2"), null))

    val tmr2 = TableMapRows(tab, tableRefStruct(tab.typ, "row.1", "row.2"))
    checkMemo(tmr2, subsetTable(tmr2.typ, "row.foo", "NO_KEY"), Array(subsetTable(tab.typ, "row.1", "row.2", "NO_KEY"), null))
  }

  @Test def testTableMapGlobalsMemo() {
    val tmg = TableMapGlobals(tab, tableRefStruct(tab.typ, "global.g1"))
    checkMemo(tmg, subsetTable(tmg.typ, "global.foo"), Array(subsetTable(tab.typ, "global.g1"), null))
  }

  @Test def testMatrixColsTableMemo() {
    val mct = MatrixColsTable(mat)
    checkMemo(mct,
      subsetTable(mct.typ, "global.g1", "row.c2"),
      Array(subsetMatrixTable(mat.typ, "global.g1", "sa.c2", "NO_ROW_KEY")))
  }

  @Test def testMatrixRowsTableMemo() {
    val mrt = MatrixRowsTable(mat)
    checkMemo(mrt,
      subsetTable(mrt.typ, "global.g1", "row.r2"),
      Array(subsetMatrixTable(mat.typ, "global.g1", "va.r2", "NO_COL_KEY")))
  }

  @Test def testMatrixEntriesTableMemo() {
    val met = MatrixEntriesTable(mat)
    checkMemo(met,
      subsetTable(met.typ, "global.g1", "row.r2", "row.c2", "row.e2"),
      Array(subsetMatrixTable(mat.typ, "global.g1", "va.r2", "sa.c2", "g.e2")))
  }

  @Test def testTableKeyByAndAggregateMemo() {
    val tka = TableKeyByAndAggregate(tab,
      tableRefStruct(tab.typ, "row.2"),
      MakeStruct(FastSeq("bar" -> tableRefBoolean(tab.typ, "row.3"))),
      None,
      1)

    checkMemo(tka, subsetTable(tka.typ, "row.foo"), Array(subsetTable(tab.typ, "row.2", "row.3", "NO_KEY"), null, null))
    checkMemo(tka, subsetTable(tka.typ), Array(subsetTable(tab.typ, "row.3", "NO_KEY"), null, null))
  }

  @Test def testTableUnionMemo() {
    checkMemo(
      TableUnion(FastIndexedSeq(tab, tab)),
      subsetTable(tab.typ, "row.1", "global.g1"),
      Array(subsetTable(tab.typ, "row.1", "global.g1"),
        subsetTable(tab.typ, "row.1", "global.g1"))
    )
  }

  @Test def testTableOrderByMemo() {
    val tob = TableOrderBy(tab, Array(SortField("2", Ascending)))
    checkMemo(tob, subsetTable(tob.typ), Array(subsetTable(tab.typ, "row.2", "row.2.2A", "NO_KEY")))

    val tob2 = TableOrderBy(tab, Array(SortField("3", Ascending)))
    checkMemo(tob2, subsetTable(tob2.typ), Array(subsetTable(tab.typ)))
  }

  @Test def testCastMatrixToTableMemo() {
    val m2t = CastMatrixToTable(mat, "__entries", "__cols")
    checkMemo(m2t,
      subsetTable(m2t.typ, "row.r2", "global.__cols.c2", "global.g2", "row.__entries.e2"),
      Array(subsetMatrixTable(mat.typ, "va.r2", "global.g2", "sa.c2", "g.e2", "NO_COL_KEY"))
    )
  }

  @Test def testMatrixFilterColsMemo() {
    val mfc = MatrixFilterCols(mat, matrixRefBoolean(mat.typ, "global.g1", "sa.c2"))
    checkMemo(mfc,
      subsetMatrixTable(mfc.typ, "sa.c3"),
      Array(subsetMatrixTable(mat.typ, "global.g1", "sa.c2", "sa.c3"), null))
  }

  @Test def testMatrixFilterRowsMemo() {
    val mfr = MatrixFilterRows(mat, matrixRefBoolean(mat.typ, "global.g1", "va.r2"))
    checkMemo(mfr,
      subsetMatrixTable(mfr.typ, "sa.c3", "va.r3"),
      Array(subsetMatrixTable(mat.typ, "global.g1", "va.r2", "sa.c3", "va.r3"), null))
  }

  @Test def testMatrixFilterEntriesMemo() {
    val mfe = MatrixFilterEntries(mat, matrixRefBoolean(mat.typ, "global.g1", "va.r2", "sa.c2", "g.e2"))
    checkMemo(mfe,
      subsetMatrixTable(mfe.typ, "sa.c3", "va.r3"),
      Array(subsetMatrixTable(mat.typ, "global.g1", "va.r2", "sa.c3", "sa.c2", "va.r3", "g.e2"), null))
  }

  @Test def testMatrixMapColsMemo() {
    val mmc = MatrixMapCols(mat, matrixRefStruct(mat.typ, "global.g1", "sa.c2", "va.r2", "g.e2"), Some(FastIndexedSeq()))
    checkMemo(mmc, subsetMatrixTable(mmc.typ, "va.r3", "sa.foo"),
      Array(subsetMatrixTable(mat.typ, "global.g1", "sa.c2", "va.r2", "g.e2", "va.r3", "NO_COL_KEY"), null))
    val mmc2 = MatrixMapCols(mat, MakeStruct(FastSeq(
      ("ck" -> GetField(Ref("sa", mat.typ.colType), "ck")),
        ("foo",matrixRefStruct(mat.typ, "global.g1", "sa.c2", "va.r2", "g.e2")))), None)
    checkMemo(mmc2, subsetMatrixTable(mmc2.typ, "va.r3", "sa.foo.foo"),
      Array(subsetMatrixTable(mat.typ, "global.g1", "sa.c2", "va.r2", "g.e2", "va.r3"), null))
  }

  @Test def testMatrixMapRowsMemo() {
    val mmr = MatrixMapRows(
      MatrixKeyRowsBy(mat, IndexedSeq.empty),
      matrixRefStruct(mat.typ, "global.g1", "sa.c2", "va.r2", "g.e2"))
    checkMemo(mmr, subsetMatrixTable(mmr.typ, "sa.c3", "va.foo"),
      Array(subsetMatrixTable(mat.typ.copy(rowKey = IndexedSeq.empty), "global.g1", "sa.c2", "va.r2", "g.e2", "sa.c3"), null))
  }

  @Test def testMatrixMapGlobalsMemo() {
    val mmg = MatrixMapGlobals(mat, matrixRefStruct(mat.typ, "global.g1"))
    checkMemo(mmg, subsetMatrixTable(mmg.typ, "global.foo", "va.r3", "sa.c3"),
      Array(subsetMatrixTable(mat.typ, "global.g1", "va.r3", "sa.c3"), null))
  }

  @Test def testMatrixAnnotateRowsTableMemo() {
    val tl = TableLiteral(Interpret(MatrixRowsTable(mat), ctx), ctx)
    val mart = MatrixAnnotateRowsTable(mat, tl, "foo", product=false)
    checkMemo(mart, subsetMatrixTable(mart.typ, "va.foo.r3", "va.r3"),
      Array(subsetMatrixTable(mat.typ, "va.r3"), subsetTable(tl.typ, "row.r3")))
  }

  @Test def testCollectColsByKeyMemo() {
    val ccbk = MatrixCollectColsByKey(mat)
    checkMemo(ccbk,
      subsetMatrixTable(ccbk.typ, "g.e2", "sa.c2"),
      Array(subsetMatrixTable(mat.typ, "g.e2", "sa.c2")))
  }

  @Test def testMatrixExplodeRowsMemo() {
    val mer = MatrixExplodeRows(mat, FastIndexedSeq("r3"))
    checkMemo(mer,
      subsetMatrixTable(mer.typ, "va.r2"),
      Array(subsetMatrixTable(mat.typ, "va.r2", "va.r3")))
  }

  @Test def testMatrixRepartitionMemo() {
    checkMemo(
      MatrixRepartition(mat, 10, RepartitionStrategy.SHUFFLE),
      subsetMatrixTable(mat.typ, "va.r2", "global.g1"),
      Array(subsetMatrixTable(mat.typ, "va.r2", "global.g1"),
        subsetMatrixTable(mat.typ, "va.r2", "global.g1"))
    )
  }

  @Test def testMatrixUnionRowsMemo() {
    checkMemo(
      MatrixUnionRows(FastIndexedSeq(mat, mat)),
      subsetMatrixTable(mat.typ, "va.r2", "global.g1"),
      Array(subsetMatrixTable(mat.typ, "va.r2", "global.g1"),
        subsetMatrixTable(mat.typ, "va.r2", "global.g1"))
    )
  }

  @Test def testMatrixDistinctByRowMemo() {
    checkMemo(
      MatrixDistinctByRow(mat),
      subsetMatrixTable(mat.typ, "va.r2", "global.g1"),
      Array(subsetMatrixTable(mat.typ, "va.r2", "global.g1"),
        subsetMatrixTable(mat.typ, "va.r2", "global.g1"))
    )
  }

  @Test def testMatrixExplodeColsMemo() {
    val mer = MatrixExplodeCols(mat, FastIndexedSeq("c3"))
    checkMemo(mer,
      subsetMatrixTable(mer.typ, "va.r2"),
      Array(subsetMatrixTable(mat.typ, "va.r2", "sa.c3")))
  }

  @Test def testCastTableToMatrixMemo() {
    val m2t = CastMatrixToTable(mat, "__entries", "__cols")
    val t2m = CastTableToMatrix(m2t, "__entries", "__cols", FastIndexedSeq("ck"))
    checkMemo(t2m,
      subsetMatrixTable(mat.typ, "va.r2", "sa.c2", "global.g2", "g.e2"),
      Array(subsetTable(m2t.typ, "row.r2", "global.g2", "global.__cols.ck", "global.__cols.c2", "row.__entries.e2"))
    )
  }

  @Test def testMatrixAggregateRowsByKeyMemo() {
    val magg = MatrixAggregateRowsByKey(mat,
      matrixRefStruct(mat.typ, "g.e2", "va.r2", "sa.c2"),
      matrixRefStruct(mat.typ, "va.r3", "global.g1"))
    checkMemo(magg,
      subsetMatrixTable(magg.typ, "sa.c3", "g.foo", "va.foo"),
      Array(subsetMatrixTable(mat.typ, "sa.c3", "g.e2", "va.r2", "sa.c2", "global.g1", "va.r3"), null, null)
    )
  }

  @Test def testMatrixAggregateColsByKeyMemo() {
    val magg = MatrixAggregateColsByKey(mat,
      matrixRefStruct(mat.typ, "g.e2", "va.r2", "sa.c2"),
      matrixRefStruct(mat.typ, "sa.c3", "global.g1"))
    checkMemo(magg,
      subsetMatrixTable(magg.typ, "va.r3", "g.foo", "sa.foo"),
      Array(subsetMatrixTable(mat.typ, "sa.c2", "va.r2", "va.r3", "g.e2", "global.g1", "sa.c3"), null, null))
  }

  val ref = Ref("x", TStruct("a" -> TInt32(), "b" -> TInt32(), "c" -> TInt32()))
  val arr = MakeArray(FastIndexedSeq(ref, ref), TArray(ref.typ))
  val empty = TStruct()
  val justA = TStruct("a" -> TInt32())
  val justB = TStruct("b" -> TInt32())

  @Test def testIfMemo() {
    checkMemo(If(True(), ref, ref),
      justA,
      Array(TBoolean(), justA, justA))
  }

  @Test def testCoalesceMemo() {
    checkMemo(Coalesce(FastSeq(ref, ref)),
      justA,
      Array(justA, justA))
  }

  @Test def testLetMemo() {
    checkMemo(Let("foo", ref, Ref("foo", ref.typ)), justA, Array(justA, null))
    checkMemo(Let("foo", ref, True()), TBoolean(), Array(empty, null))
  }

  @Test def testAggLetMemo() {
    checkMemo(AggLet("foo", ref,
      ApplyAggOp(FastIndexedSeq(), None, FastIndexedSeq(
        SelectFields(Ref("foo", ref.typ), Seq("a"))),
        AggSignature(Collect(), FastIndexedSeq(), None, FastIndexedSeq(ref.typ))), false),
      TArray(justA), Array(justA, null))
    checkMemo(AggLet("foo", ref, True(), false), TBoolean(), Array(empty, null))
  }

  @Test def testMakeArrayMemo() {
    checkMemo(arr, TArray(justB), Array(justB, justB))
  }

  @Test def testArrayRefMemo() {
    checkMemo(ArrayRef(arr, I32(0)), justB, Array(TArray(justB), null))
  }

  @Test def testArrayLenMemo() {
    checkMemo(ArrayLen(arr), TInt32(), Array(TArray(empty)))
  }

  @Test def testArrayMapMemo() {
    checkMemo(ArrayMap(arr, "foo", Ref("foo", ref.typ)),
      TArray(justB), Array(TArray(justB), null))
  }

  @Test def testArrayFilterMemo() {
    checkMemo(ArrayFilter(arr, "foo", Let("foo2", GetField(Ref("foo", ref.typ), "b"), False())),
      TArray(empty), Array(TArray(justB), null))
    checkMemo(ArrayFilter(arr, "foo", False()),
      TArray(empty), Array(TArray(empty), null))
    checkMemo(ArrayFilter(arr, "foo", False()),
      TArray(justB), Array(TArray(justB), null))
  }

  @Test def testArrayFlatMapMemo() {
    checkMemo(ArrayFlatMap(arr, "foo", MakeArray(FastIndexedSeq(Ref("foo", ref.typ)), TArray(ref.typ))),
      TArray(justA),
      Array(TArray(justA), null))
  }

  @Test def testArrayFoldMemo() {
    checkMemo(ArrayFold(arr, I32(0), "comb", "foo", GetField(Ref("foo", ref.typ), "a")),
      TInt32(),
      Array(TArray(justA), null, null))
  }

  @Test def testArrayScanMemo() {
    checkMemo(ArrayScan(arr, I32(0), "comb", "foo", GetField(Ref("foo", ref.typ), "a")),
      TArray(TInt32()),
      Array(TArray(justA), null, null))
  }

  @Test def testArrayLeftJoinDistinct() {
    val l = Ref("l", ref.typ)
    val r = Ref("r", ref.typ)
    checkMemo(ArrayLeftJoinDistinct(arr, arr, "l", "r",
      ApplyComparisonOp(LT(TInt32()), GetField(l, "a"), GetField(r, "a")),
      MakeStruct(FastIndexedSeq("a" -> GetField(l, "a"), "b" -> GetField(l, "b"), "c" -> GetField(l, "c"), "d" -> GetField(r, "b"), "e" -> GetField(r, "c")))),
      TArray(justA),
      Array(TArray(justA), TArray(justA), null, justA))
  }

  @Test def testArrayForMemo() {
    checkMemo(ArrayFor(arr, "foo", Begin(FastIndexedSeq(GetField(Ref("foo", ref.typ), "a")))),
      TVoid,
      Array(TArray(justA), null))
  }

  @Test def testMakeStructMemo() {
    checkMemo(MakeStruct(Seq("a" -> ref, "b" -> I32(10))),
      TStruct("a" -> justA), Array(justA, null))
    checkMemo(MakeStruct(Seq("a" -> ref, "b" -> I32(10))),
      TStruct(), Array(null, null))
  }

  @Test def testInsertFieldsMemo() {
    checkMemo(InsertFields(ref, Seq("d" -> ref)),
      justA ++ TStruct("d" -> justB),
      Array(justA, justB))
  }

  @Test def testSelectFieldsMemo() {
    checkMemo(SelectFields(ref, Seq("a", "b")), justA, Array(justA))
  }

  @Test def testGetFieldMemo() {
    checkMemo(GetField(ref, "a"), TInt32(), Array(justA))
  }

  @Test def testMakeTupleMemo() {
    checkMemo(MakeTuple(Seq(0 -> ref)), TTuple(justA), Array(justA))
  }

  @Test def testGetTupleElementMemo() {
    checkMemo(GetTupleElement(MakeTuple.ordered(Seq(ref, ref)), 1), justB, Array(TTuple(FastIndexedSeq(TupleField(1, justB)))))
  }

  @Test def testCastRenameMemo() {
    checkMemo(
      CastRename(
        Ref("x", TArray(TStruct("x" -> TInt32(), "y" -> TString()))),
        TArray(TStruct("y" -> TInt32(), "z" -> TString()))),
      TArray(TStruct("z" -> TString())),
      Array(TArray(TStruct("y" -> TString())))
    )
  }

  @Test def testAggFilterMemo(): Unit = {
    val t = TStruct("a" -> TInt32(), "b" -> TInt64(), "c" -> TString())
    val select = SelectFields(Ref("x", t), Seq("c"))
    checkMemo(AggFilter(
      ApplyComparisonOp(LT(TInt32(), TInt32()), GetField(Ref("x", t), "a"), I32(0)),
      ApplyAggOp(FastIndexedSeq(), None, FastIndexedSeq(select),
        AggSignature(Collect(), FastIndexedSeq(), None, FastIndexedSeq(select.typ))),
      false),
      TArray(TStruct("c" -> TString())),
      Array(null, TArray(TStruct("c" -> TString()))))
  }

  @Test def testAggExplodeMemo(): Unit = {
    val t = TArray(TStruct("a" -> TInt32(), "b" -> TInt64()))
    val select = SelectFields(Ref("foo", t.elementType), Seq("a"))
    checkMemo(AggExplode(Ref("x", t),
      "foo",
      ApplyAggOp(FastIndexedSeq(), None, FastIndexedSeq(select),
        AggSignature(Collect(), FastIndexedSeq(), None, FastIndexedSeq(select.typ))),
      false),
      TArray(TStruct("a" -> TInt32())),
      Array(TArray(TStruct("a" -> TInt32())),
        TArray(TStruct("a" -> TInt32()))))
  }

  @Test def testAggArrayPerElementMemo(): Unit = {
    val t = TArray(TStruct("a" -> TInt32(), "b" -> TInt64()))
    val select = SelectFields(Ref("foo", t.elementType), Seq("a"))
    checkMemo(AggArrayPerElement(Ref("x", t),
      "foo",
      "bar",
      ApplyAggOp(FastIndexedSeq(), None, FastIndexedSeq(select),
        AggSignature(Collect(), FastIndexedSeq(), None, FastIndexedSeq(select.typ))),
      None,
      false),
      TArray(TArray(TStruct("a" -> TInt32()))),
      Array(TArray(TStruct("a" -> TInt32())),
        TArray(TStruct("a" -> TInt32()))))
  }

  @Test def testTableCountMemo() {
    checkMemo(TableCount(tab), TInt64(), Array(subsetTable(tab.typ, "NO_KEY")))
  }

  @Test def testTableGetGlobalsMemo() {
    checkMemo(TableGetGlobals(tab), TStruct("g1" -> TInt32()), Array(subsetTable(tab.typ, "global.g1", "NO_KEY")))
  }

  @Test def testTableCollectMemo() {
    checkMemo(
      TableCollect(tab),
      TStruct("rows" -> TArray(TStruct("3" -> TString())), "global" -> TStruct("g2" -> TInt32())),
      Array(subsetTable(tab.typ, "row.3", "global.g2")))
  }

  @Test def testTableHeadMemo() {
    checkMemo(
      TableHead(tab, 10L),
      subsetTable(tab.typ.copy(key = FastIndexedSeq()), "global.g1"),
      Array(subsetTable(tab.typ, "row.3", "global.g1")))
  }

  @Test def testTableTailMemo() {
    checkMemo(
      TableTail(tab, 10L),
      subsetTable(tab.typ.copy(key = FastIndexedSeq()), "global.g1"),
      Array(subsetTable(tab.typ, "row.3", "global.g1")))
  }

  @Test def testTableToValueApplyMemo() {
    checkMemo(
      TableToValueApply(tab, ForceCountTable()),
      TInt64(),
      Array(tab.typ)
    )
  }

  @Test def testMatrixToValueApplyMemo() {
    checkMemo(
      MatrixToValueApply(mat, ForceCountMatrixTable()),
      TInt64(),
      Array(mat.typ)
    )
  }

  @Test def testTableAggregateMemo() {
    checkMemo(TableAggregate(tab, tableRefBoolean(tab.typ, "global.g1")),
      TBoolean(),
      Array(subsetTable(tab.typ, "global.g1"), null))
  }

  @Test def testMatrixAggregateMemo() {
    checkMemo(MatrixAggregate(mat, matrixRefBoolean(mat.typ, "global.g1")),
      TBoolean(),
      Array(subsetMatrixTable(mat.typ, "global.g1", "NO_COL_KEY"), null))
  }

  @Test def testPipelineLetMemo() {
    val t = TStruct("a" -> TInt32())
    checkMemo(RelationalLet("foo", NA(t), RelationalRef("foo", t)), TStruct(), Array(TStruct(), TStruct()))
  }

  @Test def testTableFilterRebuild() {
    checkRebuild(TableFilter(tr, tableRefBoolean(tr.typ, "row.2")), subsetTable(tr.typ, "row.3"),
      (_: BaseIR, r: BaseIR) => {
        val tf = r.asInstanceOf[TableFilter]
        TypeCheck(tf.pred, PruneDeadFields.relationalTypeToEnv(tf.typ))
        tf.child.typ == subsetTable(tr.typ, "row.3", "row.2")
      })
  }

  @Test def testTableMapRowsRebuild() {
    val tmr = TableMapRows(tr, tableRefStruct(tr.typ, "row.2", "global.g1"))
    checkRebuild(tmr, subsetTable(tmr.typ, "row.foo"),
      (_: BaseIR, r: BaseIR) => {
        val tmr = r.asInstanceOf[TableMapRows]
        TypeCheck(tmr.newRow, PruneDeadFields.relationalTypeToEnv(tmr.child.typ))
        tmr.child.typ == subsetTable(tr.typ, "row.2", "global.g1", "row.3")
      })

    val tmr2 = TableMapRows(tr, tableRefStruct(tr.typ, "row.2", "global.g1"))
    checkRebuild(tmr2, subsetTable(tmr2.typ, "row.foo", "NO_KEY"),
      (_: BaseIR, r: BaseIR) => {
        val tmr = r.asInstanceOf[TableMapRows]
        TypeCheck(tmr.newRow, PruneDeadFields.relationalTypeToEnv(tmr.child.typ))
        tmr.child.typ == subsetTable(tr.typ, "row.2", "global.g1", "row.3", "NO_KEY") // FIXME: remove row.3 when TableRead is fixed
      })

  }

  @Test def testTableMapGlobalsRebuild() {
    val tmg = TableMapGlobals(tr, tableRefStruct(tr.typ, "global.g1"))
    checkRebuild(tmg, subsetTable(tmg.typ, "global.foo"),
      (_: BaseIR, r: BaseIR) => {
        val tmg = r.asInstanceOf[TableMapGlobals]
        TypeCheck(tmg.newGlobals, PruneDeadFields.relationalTypeToEnv(tmg.child.typ))
        tmg.child.typ == subsetTable(tr.typ, "global.g1")
      })
  }

  @Test def testTableLeftJoinRightDistinctRebuild() {
    val tk1 = TableKeyBy(tab, Array("1"))
    val tk2 = TableKeyBy(tab, Array("3"))
    val tj = TableLeftJoinRightDistinct(tk1, tk2, "foo")

    checkRebuild(tj, subsetTable(tj.typ, "row.1", "row.4"),
      (_: BaseIR, r: BaseIR) => {
        r.isInstanceOf[TableKeyBy] // no dependence on row.foo elides the join
      })
  }

  @Test def testTableIntervalJoinRebuild() {
    val tk1 = TableKeyBy(tab, Array("1"))
    val tk2 = TableKeyBy(tab, Array("3"))
    val tj = TableIntervalJoin(tk1, tk2, "foo", product=false)

    checkRebuild(tj, subsetTable(tj.typ, "row.1", "row.4"),
      (_: BaseIR, r: BaseIR) => {
        r.isInstanceOf[TableKeyBy] // no dependence on row.foo elides the join
      })
  }

  @Test def testTableUnionRebuildUnifiesRowTypes() {
    val mapExpr = InsertFields(Ref("row", tr.typ.rowType),
      FastIndexedSeq("foo" -> tableRefBoolean(tr.typ, "row.3", "global.g1")))
    val tfilter = TableFilter(
      TableMapRows(tr, mapExpr),
      tableRefBoolean(tr.typ, "row.2"))
    val tmap = TableMapRows(tr, mapExpr)
    val tunion = TableUnion(FastIndexedSeq(tfilter, tmap))
    checkRebuild(tunion, subsetTable(tunion.typ, "row.foo"),
      (_: BaseIR, rebuilt: BaseIR) => {
        val tu = rebuilt.asInstanceOf[TableUnion]
        val tf = tu.children(0)
        val tm = tu.children(1)
        tf.typ.rowType == tm.typ.rowType &&
          tu.typ == subsetTable(tunion.typ, "row.foo", "global.g1")
      })
  }

  @Test def testTableMultiWayZipJoinRebuildUnifiesRowTypes() {
    val t1 = TableKeyBy(tab, Array("1"))
    val t2 = TableFilter(t1, tableRefBoolean(t1.typ, "row.2"))
    val t3 = TableFilter(t1, tableRefBoolean(t1.typ, "row.3"))
    val ts = Array(t1, t2, t3)
    val tmwzj = TableMultiWayZipJoin(ts, "data", "gbls")
    val childRType = subsetTable(t1.typ, "row.2", "global.g1")
    checkRebuild(tmwzj, subsetTable(tmwzj.typ, "row.data.2", "global.gbls.g1"),
      (_: BaseIR, rebuilt: BaseIR) => {
        val t = rebuilt.asInstanceOf[TableMultiWayZipJoin]
        t.children.forall { c => c.typ == childRType }
      })
  }


  @Test def testMatrixFilterColsRebuild() {
    val mfc = MatrixFilterCols(mr, matrixRefBoolean(mr.typ, "sa.c2"))
    checkRebuild(mfc, subsetMatrixTable(mfc.typ, "global.g1"),
      (_: BaseIR, r: BaseIR) => {
        val mfc = r.asInstanceOf[MatrixFilterCols]
        TypeCheck(mfc.pred, PruneDeadFields.relationalTypeToEnv(mfc.child.typ))
        mfc.child.asInstanceOf[MatrixRead].typ == subsetMatrixTable(mr.typ, "global.g1", "sa.c2")
      }
    )
  }

  @Test def testMatrixFilterEntriesRebuild() {
    val mfe = MatrixFilterEntries(mr, matrixRefBoolean(mr.typ, "sa.c2", "va.r2", "g.e1"))
    checkRebuild(mfe, subsetMatrixTable(mfe.typ, "global.g1"),
      (_: BaseIR, r: BaseIR) => {
        val mfe = r.asInstanceOf[MatrixFilterEntries]
        TypeCheck(mfe.pred, PruneDeadFields.relationalTypeToEnv(mfe.child.typ))
        mfe.child.asInstanceOf[MatrixRead].typ == subsetMatrixTable(mr.typ, "global.g1", "sa.c2", "va.r2", "g.e1")
      }
    )
  }

  @Test def testMatrixMapRowsRebuild() {
    val mmr = MatrixMapRows(
      MatrixKeyRowsBy(mr, IndexedSeq.empty),
      matrixRefStruct(mr.typ, "va.r2"))
    checkRebuild(mmr, subsetMatrixTable(mmr.typ, "global.g1", "g.e1", "va.foo"),
      (_: BaseIR, r: BaseIR) => {
        val mmr = r.asInstanceOf[MatrixMapRows]
        TypeCheck(mmr.newRow, PruneDeadFields.relationalTypeToEnv(mmr.child.typ))
        mmr.child.asInstanceOf[MatrixKeyRowsBy].child.asInstanceOf[MatrixRead].typ == subsetMatrixTable(mr.typ, "global.g1", "va.r2", "g.e1")
      }
    )
  }

  @Test def testMatrixMapColsRebuild() {
    val mmc = MatrixMapCols(mr, matrixRefStruct(mr.typ, "sa.c2"),
      Some(FastIndexedSeq("foo")))
    checkRebuild(mmc, subsetMatrixTable(mmc.typ, "global.g1", "g.e1", "sa.foo"),
      (_: BaseIR, r: BaseIR) => {
        val mmc = r.asInstanceOf[MatrixMapCols]
        TypeCheck(mmc.newCol, PruneDeadFields.relationalTypeToEnv(mmc.child.typ))
        mmc.child.asInstanceOf[MatrixRead].typ == subsetMatrixTable(mr.typ, "global.g1", "sa.c2", "g.e1")
      }
    )
  }

  @Test def testMatrixMapEntriesRebuild() {
    val mme = MatrixMapEntries(mr, matrixRefStruct(mr.typ, "sa.c2", "va.r2"))
    checkRebuild(mme, subsetMatrixTable(mme.typ, "global.g1", "g.foo"),
      (_: BaseIR, r: BaseIR) => {
        val mme = r.asInstanceOf[MatrixMapEntries]
        TypeCheck(mme.newEntries, PruneDeadFields.relationalTypeToEnv(mme.child.typ))
        mme.child.asInstanceOf[MatrixRead].typ == subsetMatrixTable(mr.typ, "global.g1", "sa.c2", "va.r2")
      }
    )
  }

  @Test def testMatrixMapGlobalsRebuild() {
    val mmg = MatrixMapGlobals(mr, matrixRefStruct(mr.typ, "global.g1"))
    checkRebuild(mmg, subsetMatrixTable(mmg.typ, "global.foo", "g.e1", "va.r2"),
      (_: BaseIR, r: BaseIR) => {
        val mmg = r.asInstanceOf[MatrixMapGlobals]
        TypeCheck(mmg.newGlobals, PruneDeadFields.relationalTypeToEnv(mmg.child.typ))
        mmg.child.asInstanceOf[MatrixRead].typ == subsetMatrixTable(mr.typ, "global.g1", "va.r2", "g.e1")
      }
    )
  }

  @Test def testMatrixAggregateRowsByKeyRebuild() {
    val ma = MatrixAggregateRowsByKey(mr, matrixRefStruct(mr.typ, "sa.c2"), matrixRefStruct(mr.typ, "global.g1"))
    checkRebuild(ma, subsetMatrixTable(ma.typ, "va.foo", "g.foo"),
      (_: BaseIR, r: BaseIR) => {
        val ma = r.asInstanceOf[MatrixAggregateRowsByKey]
        TypeCheck(ma.entryExpr, PruneDeadFields.relationalTypeToEnv(ma.child.typ))
        ma.child.asInstanceOf[MatrixRead].typ == subsetMatrixTable(mr.typ, "global.g1", "sa.c2")
      }
    )
  }

  @Test def testMatrixAggregateColsByKeyRebuild() {
    val ma = MatrixAggregateColsByKey(mr, matrixRefStruct(mr.typ, "va.r2"), matrixRefStruct(mr.typ, "global.g1"))
    checkRebuild(ma, subsetMatrixTable(ma.typ, "g.foo", "sa.foo"),
      (_: BaseIR, r: BaseIR) => {
        val ma = r.asInstanceOf[MatrixAggregateColsByKey]
        TypeCheck(ma.entryExpr, PruneDeadFields.relationalTypeToEnv(ma.child.typ))
        ma.child.asInstanceOf[MatrixRead].typ == subsetMatrixTable(mr.typ, "global.g1", "va.r2")
      }
    )
  }

  @Test def testMatrixAnnotateRowsTableRebuild() {
    val tl = TableLiteral(Interpret(MatrixRowsTable(mat), ctx), ctx)
    val mart = MatrixAnnotateRowsTable(mat, tl, "foo", product=false)
    checkRebuild(mart, subsetMatrixTable(mart.typ),
      (_: BaseIR, r: BaseIR) => {
        r.isInstanceOf[MatrixLiteral]
      })
  }

  val ts = TStruct(
    "a" -> TInt32(),
    "b" -> TInt64(),
    "c" -> TString()
  )

  def subsetTS(fields: String*): TStruct = ts.filterSet(fields.toSet)._1

  @Test def testNARebuild() {
    checkRebuild(NA(ts), subsetTS("b"),
      (_: BaseIR, r: BaseIR) => {
        val na = r.asInstanceOf[NA]
        na.typ == subsetTS("b")
      })
  }

  @Test def testIfRebuild() {
    checkRebuild(If(True(), NA(ts), NA(ts)), subsetTS("b"),
      (_: BaseIR, r: BaseIR) => {
        val ir = r.asInstanceOf[If]
        ir.cnsq.typ == subsetTS("b") && ir.altr.typ == subsetTS("b")
      })
  }

  @Test def testCoalesceRebuild() {
    checkRebuild(Coalesce(FastSeq(NA(ts), NA(ts))), subsetTS("b"),
      (_: BaseIR, r: BaseIR) => {
        r.children.forall(_.typ == subsetTS("b"))
      })
  }

  @Test def testLetRebuild() {
    checkRebuild(Let("x", NA(ts), Ref("x", ts)), subsetTS("b"),
      (_: BaseIR, r: BaseIR) => {
        val ir = r.asInstanceOf[Let]
        ir.value.typ == subsetTS("b")
      })
  }

  @Test def testAggLetRebuild() {
    checkRebuild(AggLet("foo", NA(ref.typ),
      ApplyAggOp(FastIndexedSeq(), None, FastIndexedSeq(
        SelectFields(Ref("foo", ref.typ), Seq("a"))),
        AggSignature(Collect(), FastIndexedSeq(), None, FastIndexedSeq(ref.typ))), false), subsetTS("b"),
      (_: BaseIR, r: BaseIR) => {
        val ir = r.asInstanceOf[AggLet]
        ir.value.typ == subsetTS("a")
      })
  }

  @Test def testMakeArrayRebuild() {
    checkRebuild(MakeArray(Seq(NA(ts)), TArray(ts)), TArray(subsetTS("b")),
      (_: BaseIR, r: BaseIR) => {
        val ir = r.asInstanceOf[MakeArray]
        ir.args.head.typ == subsetTS("b")
      })
  }

  @Test def testArrayMapRebuild() {
    checkRebuild(ArrayMap(MakeArray(Seq(NA(ts)), TArray(ts)), "x", Ref("x", ts)), TArray(subsetTS("b")),
      (_: BaseIR, r: BaseIR) => {
        val ir = r.asInstanceOf[ArrayMap]
        ir.a.typ == TArray(subsetTS("b"))
      })
  }

  @Test def testArrayFlatmapRebuild() {
    checkRebuild(ArrayFlatMap(MakeArray(Seq(NA(ts)), TArray(ts)), "x", MakeArray(Seq(Ref("x", ts)), TArray(ts))),
      TArray(subsetTS("b")),
      (_: BaseIR, r: BaseIR) => {
        val ir = r.asInstanceOf[ArrayFlatMap]
        ir.a.typ == TArray(subsetTS("b"))
      })
  }

  @Test def testMakeStructRebuild() {
    checkRebuild(MakeStruct(Seq("a" -> NA(TInt32()), "b" -> NA(TInt64()), "c" -> NA(TString()))), subsetTS("b"),
      (_: BaseIR, r: BaseIR) => {
        r == MakeStruct(Seq("b" -> NA(TInt64())))
      })
  }

  @Test def testInsertFieldsRebuild() {
    checkRebuild(InsertFields(NA(TStruct("a" -> TInt32())), Seq("b" -> NA(TInt64()), "c" -> NA(TString()))),
      subsetTS("b"),
      (_: BaseIR, r: BaseIR) => {
        val ir = r.asInstanceOf[InsertFields]
        ir.fields == Seq(
          "b" -> NA(TInt64())
        )
      })
  }

  @Test def testMakeTupleRebuild() {
    checkRebuild(MakeTuple(Seq(0 -> I32(1), 1 -> F64(1.0), 2 -> NA(TString()))),
      TTuple(FastIndexedSeq(TupleField(2, TString()))),
    (_: BaseIR, r: BaseIR) => {
      r == MakeTuple(Seq(2 -> NA(TString())))
    })
  }

  @Test def testSelectFieldsRebuild() {
    checkRebuild(SelectFields(NA(ts), Seq("a", "b")),
      subsetTS("b"),
      (_: BaseIR, r: BaseIR) => {
        val ir = r.asInstanceOf[SelectFields]
        ir.fields == Seq("b")
      })
  }

  @Test def testCastRenameRebuild() {
    checkRebuild(
      CastRename(
        NA(TArray(TStruct("x" -> TInt32(), "y" -> TString()))),
        TArray(TStruct("y" -> TInt32(), "z" -> TString()))),
      TArray(TStruct("z" -> TString())),
      (_: BaseIR, r: BaseIR) => {
        val ir = r.asInstanceOf[CastRename]
        ir._typ == TArray(TStruct("z" -> TString()))
      })
  }

  @Test def testTableAggregateRebuild() {
    val ta = TableAggregate(tr, tableRefBoolean(tr.typ, "row.2"))
    checkRebuild(ta, TBoolean(),
      (_: BaseIR, r: BaseIR) => {
        val ir = r.asInstanceOf[TableAggregate]
        ir.child.typ == subsetTable(tr.typ, "row.2")
      })
  }

  @Test def testTableCollectRebuild() {
    val tc = TableCollect(TableKeyBy(tab, FastIndexedSeq()))
    checkRebuild(tc, TStruct("global" -> TStruct("g1" -> TInt32())),
      (_: BaseIR, r: BaseIR) => {
        r.asInstanceOf[MakeStruct].fields.head._2.isInstanceOf[TableGetGlobals]
      })

    checkRebuild(tc, TStruct(),
      (_: BaseIR, r: BaseIR) => {
        r == MakeStruct(Seq())
      })
  }

  @Test def testMatrixAggregateRebuild() {
    val ma = MatrixAggregate(mr, matrixRefBoolean(mr.typ, "va.r2"))
    checkRebuild(ma, TBoolean(),
      (_: BaseIR, r: BaseIR) => {
        val ir = r.asInstanceOf[MatrixAggregate]
        ir.child.typ == subsetMatrixTable(mr.typ, "va.r2")
      })
  }

  @Test def testPipelineLetRebuild() {
    val t = TStruct("a" -> TInt32())
    checkRebuild(RelationalLet("foo", NA(t), RelationalRef("foo", t)), TStruct(),
      (_: BaseIR, r: BaseIR) => {
        r.asInstanceOf[RelationalLet].body == RelationalRef("foo", TStruct())
      })
  }

  @Test def testPipelineLetTableRebuild() {
    val t = TStruct("a" -> TInt32())
    checkRebuild(RelationalLetTable("foo", NA(t), TableMapGlobals(tab, RelationalRef("foo", t))),
      tab.typ.copy(globalType = TStruct()),
      (_: BaseIR, r: BaseIR) => {
        r.asInstanceOf[RelationalLetTable].body.asInstanceOf[TableMapGlobals].newGlobals == RelationalRef("foo", TStruct())
      })
  }

  @Test def testPipelineLetMatrixTableRebuild() {
    val t = TStruct("a" -> TInt32())
    checkRebuild(RelationalLetMatrixTable("foo", NA(t), MatrixMapGlobals(mat, RelationalRef("foo", t))),
      mat.typ.copy(globalType = TStruct()),
      (_: BaseIR, r: BaseIR) => {
        r.asInstanceOf[RelationalLetMatrixTable].body.asInstanceOf[MatrixMapGlobals].newGlobals == RelationalRef("foo", TStruct())
      })
  }

  @Test def testIfUnification() {
    val pred = False()
    val t = TStruct("a" -> TInt32(), "b" -> TInt32())
    val pruneT = TStruct("a" -> TInt32())
    val cnsq = Ref("x", t)
    val altr = NA(t)
    val ifIR = If(pred, cnsq, altr)
    val memo = Memo.empty[BaseType]
      .bind(pred, TBoolean())
      .bind(cnsq, pruneT)
      .bind(altr, pruneT)
      .bind(ifIR, pruneT)

    // should run without error!
    PruneDeadFields.rebuildIR(ifIR, BindingEnv.empty[Type].bindEval("a", t),
      PruneDeadFields.RebuildMutableState(memo, mutable.HashMap.empty))
  }

  @DataProvider(name = "supertypePairs")
  def supertypePairs: Array[Array[Type]] = Array(
    Array(TInt32(), TInt32().setRequired(true)),
    Array(
      TStruct(
        "a" -> TInt32().setRequired(true),
        "b" -> TArray(TInt64())),
      TStruct(
        "a" -> TInt32().setRequired(true),
        "b" -> TArray(TInt64().setRequired(true)).setRequired(true))),
    Array(TSet(TString()), TSet(TString()).setRequired(true))
  )

  @Test(dataProvider = "supertypePairs")
  def testIsSupertypeRequiredness(t1: Type, t2: Type) = {
    assert(PruneDeadFields.isSupertype(t1, t2),
      s"""Failure, supertype relationship not met
         | supertype: ${ t1.toPrettyString(0, true) }
         | subtype:   ${ t2.toPrettyString(0, true) }""".stripMargin)
  }
}

