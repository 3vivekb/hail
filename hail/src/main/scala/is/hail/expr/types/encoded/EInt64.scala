package is.hail.expr.types.encoded

import is.hail.annotations.Region
import is.hail.asm4s._
import is.hail.expr.types.BaseType
import is.hail.expr.types.physical._
import is.hail.expr.types.virtual._
import is.hail.io.{InputBuffer, OutputBuffer}
import is.hail.utils._

case object EInt64Optional extends EInt64(false)
case object EInt64Required extends EInt64(true)

class EInt64(override val required: Boolean) extends EType {
  lazy val virtualType: TInt64 = TInt64(required)

  def _buildEncoder(pt: PType, mb: MethodBuilder, v: Code[_], out: Code[OutputBuffer]): Code[Unit] = {
    out.writeLong(coerce[Long](v))
  }

  def _buildDecoder(
    pt: PType,
    mb: MethodBuilder,
    region: Code[Region],
    in: Code[InputBuffer]
  ): Code[Long] = in.readLong()

  def _buildSkip(mb: MethodBuilder, r: Code[Region], in: Code[InputBuffer]): Code[Unit] = in.skipLong()

  override def _compatible(pt: PType): Boolean = pt.isInstanceOf[PInt64]

  def _decodedPType(requestedType: Type): PType = PInt64(required)

  def asIdent = "int64"
  def _toPretty = "Int64"
}

object EInt64 {
  def apply(required: Boolean = false): EInt64 = if (required) EInt64Required else EInt64Optional
}
